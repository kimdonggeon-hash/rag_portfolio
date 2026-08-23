# ragapp/news_views/policy_qa_views.py
from __future__ import annotations

import re
import json
import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

# ✅ 기존 news_views.py에 있는 유틸/응답 포맷/로그/PII 가드 등을 그대로 재사용 (파일 수정 없이 import)
from .news_views import (  # noqa
    _ok,
    _fail,
    _guard_pii_or_none,
    _pii_block_msg,
    _unpack_answer_sources,
    _env_model_rag,
    _env_model_direct,
    get_chat_session_id,
    log_chat_message,
    _hit_text,
    _normalize_rag_sources,
    _filter_hits_used_in_answer,
    _fix_invalid_citations,
    _append_chat_history,
)

# ✅ 기존 서비스 레이어 그대로 재사용
from ragapp.services.news_services import (
    gemini_answer_with_news,
    rag_answer_grounded,
    rag_answer_grounded_with_history,
)

from ragapp.services.source_quality import filter_source_cards_dicts
from ragapp.services.usage_limiter import check_and_increment_usage
from ragapp.services.safety import is_sensitive_question, safe_block_response
from ragapp.services.domain_router import decide_domain
from ragapp.services.web_hint_router import decide_web_hint
from ragapp.qa_data import find_best_faq_answer

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 정책 라우터 설정
# ─────────────────────────────────────────────

_PENDING_KEY = "qa_policy_pending"
_PENDING_TTL_MIN = int(getattr(settings, "QA_POLICY_CLARIFY_TTL_MIN", 10))

DOMAIN_THRESHOLD = float(getattr(settings, "QA_POLICY_DOMAIN_THRESHOLD", 0.65))

# RAG evidence 기준(기본값은 넉넉하게)
EVIDENCE_MIN_CARDS = int(getattr(settings, "QA_POLICY_EVIDENCE_MIN_CARDS", 2))
EVIDENCE_MIN_SCORE = getattr(settings, "QA_POLICY_EVIDENCE_MIN_SCORE", None)
try:
    EVIDENCE_MIN_SCORE = float(EVIDENCE_MIN_SCORE) if EVIDENCE_MIN_SCORE is not None else 0.20
except Exception:
    EVIDENCE_MIN_SCORE = 0.20

EVIDENCE_MIN_SNIPPET = int(getattr(settings, "QA_POLICY_EVIDENCE_MIN_SNIPPET_CHARS", 120))


def _now_iso() -> str:
    # pending 저장용(서버 기준). 굳이 localtime일 필요는 없지만 일관성 위해 localtime 사용
    return timezone.localtime().isoformat()


def _build_time_ctx() -> Dict[str, str]:
    """
    ✅ Web 모드에서 '오늘/지금/최근/이번 주' 같은 해석이 흔들리지 않도록
    요청마다 KST 기준 시간을 만들어 서비스로 전달한다.
    """
    now = timezone.localtime()  # KST
    dow_kr = ["월", "화", "수", "목", "금", "토", "일"][now.weekday()]
    return {
        "now_kst_iso": now.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "today_iso": now.strftime("%Y-%m-%d"),
        "today_kr": now.strftime("%Y년 %m월 %d일"),
        "dow_kr": dow_kr,
    }


def _is_stale(iso_ts: str) -> bool:
    try:
        t = timezone.datetime.fromisoformat(iso_ts)
        if timezone.is_naive(t):
            t = timezone.make_aware(t, timezone.get_current_timezone())
        return timezone.now() - t > timedelta(minutes=_PENDING_TTL_MIN)
    except Exception:
        return True


def _get_q_from_body(request: HttpRequest) -> str:
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST
    return (payload.get("q") or payload.get("query") or payload.get("question") or "").strip()


def _get_prefer_from_body(request: HttpRequest) -> str:
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST

    p = (payload.get("prefer") or payload.get("mode") or "").strip().lower()
    if p in ("web", "rag", "auto"):
        return p
    return "auto"


# ─────────────────────────────────────────────
# 도메인/리스크(간단 규칙)
# ─────────────────────────────────────────────

# 리스크(사생활/루머) 감지
_PRIVACY_RE = re.compile(
    r"(주소|집|전화|번호|연락처|주민|계좌|사는곳|학교|직장어디|신상|"
    r"이메일|메일|e-?mail|우편번호|postcode|zip\s*code?)",
    re.IGNORECASE,
)
_RUMOR_RE = re.compile(r"(루머|썰|카더라|뒷얘기|폭로|불륜|바람|마약|전과|범죄자|스캔들)", re.IGNORECASE)

# ✅ 모욕/비하(간단) 감지
# - "새끼" 단독은 오탐 가능(새끼손가락/동물 새끼 등)이라, 욕으로 쓰이는 형태만 잡는다.
_ABUSE_RE = re.compile(
    r"(바보|멍청(?:이)?|병신|븅신|ㅂㅅ|ㅄ|등신|개새끼|새끼야|새끼들)",
    re.IGNORECASE,
)


def _detect_abuse(q: str) -> Optional[str]:
    t = (q or "").strip()
    if not t:
        return None
    m = _ABUSE_RE.search(t)
    return m.group(0) if m else None


def _abuse_block_message() -> str:
    return "비방/모욕 표현이 포함된 요청은 처리할 수 없어. 표현을 정리해서 다시 물어봐줘."


def _norm_domain(d: str) -> str:
    """
    domain_router의 'legal'을 이 파일의 관례('law')로 맞춤.
    """
    d = (d or "").strip().lower()
    if d == "legal":
        return "law"
    if d in ("law", "medical", "person", "other"):
        return d
    return d or "other"


def _detect_risk(q: str) -> str:
    t = (q or "").strip()
    if not t:
        return "none"
    if _PRIVACY_RE.search(t):
        return "privacy"
    if _RUMOR_RE.search(t):
        return "rumor"
    return "none"


# ─────────────────────────────────────────────
# 근거 판단 / 보수 출력
# ─────────────────────────────────────────────

def _evidence_ok(hits_ui: List[Dict[str, Any]]) -> bool:
    if not hits_ui or len(hits_ui) < EVIDENCE_MIN_CARDS:
        return False

    for h in (hits_ui[:3] if len(hits_ui) > 3 else hits_ui):
        snip = (h.get("snippet") or "") if isinstance(h, dict) else ""
        sc = h.get("score") if isinstance(h, dict) else None

        if isinstance(sc, (int, float)) and float(sc) >= float(EVIDENCE_MIN_SCORE):
            return True
        if isinstance(snip, str) and len(snip.strip()) >= EVIDENCE_MIN_SNIPPET:
            return True

    return False


def _conservative_wrap(answer: str, domain: str, risk: str) -> str:
    head = "민감한 주제라 확인되지 않은 단정은 피할게.\n"
    if risk == "privacy":
        head = "사생활/신상 정보는 다룰 수 없어. 대신 안전하게 확인하는 방법만 안내할게.\n"
    elif risk == "rumor":
        head = "확인되지 않은 주장(루머)은 단정할 수 없어. 공개적으로 검증 가능한 범위에서만 말할게.\n"

    if domain.startswith("medical"):
        head += "의료는 일반 정보이며, 급격한 악화/응급 신호가 있으면 즉시 진료가 우선이야.\n"
    if domain.startswith("law"):
        head += "법률은 일반 정보이며, 구체 사건은 관할/사실관계에 따라 달라질 수 있어.\n"

    return (head + "\n" + (answer or "")).strip()


# ─────────────────────────────────────────────
# privacy “단어만” 케이스도 차단 (값이 없어도 컷)
# ─────────────────────────────────────────────

# ✅ “요청(알려줘/찾아줘)” 뿐 아니라 “뭐야/정의/설명” 같은 질문도 포함
_PRIVACY_ASK_RE = re.compile(
    r"(알려|찾아|어디|뭐야|뭔지|무엇|뜻|정의|설명|"
    r"번호|연락처|주소|사는곳|신상|학교|직장)",
    re.IGNORECASE,
)

# ✅ 단어만 던지는 케이스(값이 없어도 차단)
_PRIVACY_TERM_RE = re.compile(
    r"(이메일|메일|e-?mail|우편번호|postcode|zip\s*code?)",
    re.IGNORECASE,
)

_PRIVACY_TERM_EMAIL_RE = re.compile(r"(이메일|메일|e-?mail)", re.IGNORECASE)
_PRIVACY_TERM_POSTCODE_RE = re.compile(r"(우편번호|postcode|zip\s*code?)", re.IGNORECASE)


def _privacy_request_is_refusal(q: str) -> bool:
    """
    - 값(실제 이메일/우편번호)이 없더라도,
      '이메일/우편번호' 자체를 묻거나 요구하면 차단.
    """
    t = (q or "").strip()
    if not t:
        return False

    # 1) “알려/찾아/정의/설명/뭐야 …” + (privacy 키워드) 조합
    if _PRIVACY_ASK_RE.search(t) and _PRIVACY_RE.search(t):
        return True

    # 2) “이메일?” “우편번호 알려줘” 같은 짧은 케이스 (값 없어도 컷)
    if _PRIVACY_TERM_RE.search(t) and len(t) <= 30:
        return True

    return False


def _privacy_refusal_message(q: str, domain: str, risk: str) -> str:
    """
    privacy 요청 차단 메시지:
    - 이메일/우편번호/그 외(연락처/주소/신상)로 문구를 다르게 제공
    """
    t = (q or "").strip()

    if _PRIVACY_TERM_EMAIL_RE.search(t):
        base = (
            "이 서비스에서는 이메일처럼 개인 식별/연락 정보와 연결될 수 있는 주제는 다루지 않아.\n"
            "특정 이메일 주소 제공/추적 요청은 안전상 차단돼.\n"
            "필요하면 사용하는 서비스의 공식 도움말/고객센터 같은 공개된 안내 경로로 확인해줘."
        )
        return _conservative_wrap(base, domain=domain, risk=risk)

    if _PRIVACY_TERM_POSTCODE_RE.search(t):
        base = (
            "이 서비스에서는 우편번호처럼 주소와 결합되면 위치 식별이 가능한 정보 요청을 다루지 않아.\n"
            "특정 지역/주소의 우편번호는 공식 주소/우편 안내 시스템(공공 안내)에서 확인해줘."
        )
        return _conservative_wrap(base, domain=domain, risk=risk)

    base = (
        "특정 개인의 신상/연락처/주소 같은 정보 제공이나 추적은 도와줄 수 없어.\n"
        "대신 본인이 직접 확인 가능한 공식 경로(공식 홈페이지/고객센터/공개된 연락 창구)로 확인해줘."
    )
    return _conservative_wrap(base, domain=domain, risk=risk)


# ─────────────────────────────────────────────
# ✅ 정책 라우터 API
# ─────────────────────────────────────────────
@csrf_protect
@require_http_methods(["POST"])
def qa_policy_view(request: HttpRequest) -> JsonResponse:
    """
    단일 엔드포인트:
      - 애매하면 재질문 1회(서버 세션에 pending 저장)
      - RAG에 근거가 있으면 RAG 최종
      - 근거가 없으면 WEB 최종
      - 루머/사생활 성격이면 어떤 경우든 보수적으로 출력
    """
    q_in = _get_q_from_body(request)
    if not q_in:
        return _fail("query가 비었습니다.", status_code=400)

    prefer = _get_prefer_from_body(request)  # "web" | "rag" | "auto"
    force_web = (prefer == "web")
    force_rag = (prefer == "rag")

    # 0) pending(재질문) 합치기
    pending = request.session.get(_PENDING_KEY)
    if isinstance(pending, dict):
        asked_at = str(pending.get("asked_at") or "")
        if asked_at and not _is_stale(asked_at):
            q0 = (pending.get("q0") or "").strip()
            ask0 = (pending.get("ask") or "").strip()
            if q0:
                if ask0:
                    q = f"{q0}\n\n[추가질문]\n{ask0}\n[사용자답변]\n{q_in}"
                else:
                    q = f"{q0}\n\n[추가정보]\n{q_in}"
            else:
                q = q_in
            # 1회만 허용이므로 바로 제거
            request.session.pop(_PENDING_KEY, None)
            request.session.modified = True
        else:
            request.session.pop(_PENDING_KEY, None)
            request.session.modified = True
            q = q_in
    else:
        q = q_in

    # 1) PII 차단 (실제 값이 섞여 있으면 여기서 컷)
    blocked, kind = _guard_pii_or_none(q)
    if blocked:
        msg = _pii_block_msg(kind)
        return _ok(
            {
                "mode": "blocked",
                "msg": "PII 차단",
                "answer_text": msg,
                "answer": msg,
                "answer_html": "",
                "hits": [],
                "sources": [],
                "model": _env_model_rag(),
                "code": "PII_BLOCKED",
                "pii_kind": kind,
            }
        )

    # 2) 기본 세션/로그 준비
    session_id = get_chat_session_id(request)

    # 3) “안전 블락”(너가 기존에 쓰던 것 유지)
    if is_sensitive_question(q):
        safe_ans = safe_block_response(q)
        try:
            log_chat_message(
                request=request,
                session_id=session_id,
                channel="qarag",
                mode="blocked",
                role="user",
                message_type="query",
                question=q,
                content=q,
                sources=[],
                meta_extra={"where": "qa_policy_view", "stage": "pre_safety"},
            )
            log_chat_message(
                request=request,
                session_id=session_id,
                channel="qarag",
                mode="blocked",
                role="assistant",
                message_type="answer",
                question=q,
                content=safe_ans,
                answer_excerpt=safe_ans[:500],
                sources=[],
                meta_extra={"where": "qa_policy_view", "stage": "pre_safety"},
            )
        except Exception:
            pass

        _append_chat_history(request, q, safe_ans)

        return _ok(
            {
                "mode": "blocked",
                "model": _env_model_rag(),
                "answer_text": safe_ans,
                "answer": safe_ans,
                "answer_html": "",
                "hits": [],
                "sources": [],
                "session_id": session_id,
            }
        )

    # ✅ 3.5) 모욕/비하 차단 (도메인 판정 전에 컷)
    abuse_hit = _detect_abuse(q)
    if abuse_hit:
        msg = _abuse_block_message()
        try:
            log_chat_message(
                request=request,
                session_id=session_id,
                channel="qarag",
                mode="blocked",
                role="user",
                message_type="query",
                question=q_in,
                content=q_in,
                sources=[],
                meta_extra={"where": "qa_policy_view", "stage": "abuse_block", "abuse_hit": abuse_hit},
            )
            log_chat_message(
                request=request,
                session_id=session_id,
                channel="qarag",
                mode="blocked",
                role="assistant",
                message_type="answer",
                question=q_in,
                content=msg,
                answer_excerpt=msg[:500],
                sources=[],
                meta_extra={"where": "qa_policy_view", "stage": "abuse_block", "abuse_hit": abuse_hit},
            )
        except Exception:
            pass

        _append_chat_history(request, q_in, msg)

        return _ok(
            {
                "mode": "blocked",
                "code": "ABUSE_BLOCKED",
                "answer_text": msg,
                "answer": msg,
                "answer_html": "",
                "hits": [],
                "sources": [],
                "model": _env_model_rag(),
                "session_id": session_id,
            }
        )

    # 4) 도메인/리스크 판정
    decision = decide_domain(q)

    raw_domain = (
        (decision.get("domain") if isinstance(decision, dict) else getattr(decision, "domain", ""))
        if decision else ""
    )
    domain = _norm_domain(raw_domain) if raw_domain else "other"

    risk = _detect_risk(q)
    conservative = (risk in {"privacy", "rumor"})

    # ✅ 여기서 action/ask/message를 "한 번만" 뽑아서 재사용
    dec_action = (
        (decision.get("action") if isinstance(decision, dict) else getattr(decision, "action", None))
        if decision else None
    )
    dec_ask = (
        (decision.get("ask") if isinstance(decision, dict) else getattr(decision, "ask", ""))
        if decision else ""
    )
    dec_msg = (
        (decision.get("message") if isinstance(decision, dict) else getattr(decision, "message", ""))
        if decision else ""
    )

    # ✅ domain_router에서 바로 block 내려오면 즉시 종료
    if dec_action == "block":
        msg = dec_msg or "요청을 처리할 수 없습니다."
        return _ok(
            {
                "mode": "blocked",
                "code": "DOMAIN_BLOCKED",
                "answer_text": msg,
                "answer": msg,
                "hits": [],
                "sources": [],
                "model": _env_model_rag(),
                "session_id": session_id,
                "domain": domain,
                "risk": risk,
            }
        )

    # ✅ 사생활/신상 요청이면 차단 (값이 없어도: “이메일이 뭐야?”, “우편번호 알려줘” 포함)
    if risk == "privacy" and _privacy_request_is_refusal(q):
        msg = _privacy_refusal_message(q, domain=domain, risk=risk)
        return _ok(
            {
                "mode": "blocked",
                "code": "PRIVACY_REQUEST_BLOCKED",
                "answer_text": msg,
                "answer": msg,
                "hits": [],
                "sources": [],
                "model": _env_model_rag(),
                "session_id": session_id,
                "domain": domain,
                "risk": risk,
            }
        )

    # 5) 애매하면 재질문 1회(단, 루머/사생활은 재질문보단 보수 응답 우선)
    if (not conservative) and (dec_action == "clarify") and not request.session.get(_PENDING_KEY):
        ask = dec_ask
        request.session[_PENDING_KEY] = {
            "q0": q_in,
            "asked_at": _now_iso(),
            "domain": domain,
            "risk": risk,
            "count": 1,
            "ask": ask,
        }
        request.session.modified = True

        # 로그는 남기되, UI는 그냥 answer_text로 보여도 됨(나중에 UI 개선)
        try:
            log_chat_message(
                request=request,
                session_id=session_id,
                channel="qarag",
                mode="clarify",
                role="user",
                message_type="query",
                question=q_in,
                content=q_in,
                sources=[],
                meta_extra={"where": "qa_policy_view", "stage": "clarify"},
            )
            log_chat_message(
                request=request,
                session_id=session_id,
                channel="qarag",
                mode="clarify",
                role="assistant",
                message_type="answer",
                question=q_in,
                content=ask,
                answer_excerpt=ask[:500],
                sources=[],
                meta_extra={"where": "qa_policy_view", "stage": "clarify"},
            )
        except Exception:
            pass

        return _ok(
            {
                "mode": "clarify",
                "code": "NEED_CLARIFY",
                "ask": ask,
                "answer_text": ask,
                "answer": ask,
                "hits": [],
                "sources": [],
                "model": _env_model_rag(),
                "session_id": session_id,
                "domain": domain,
                "risk": risk,
            }
        )

    if (not force_web) and (not conservative):
        hint = decide_web_hint(q_in)  # 보통 q_in이 더 안전(재질문 합친 q로 인한 오탐 방지)
        if hint:
            msg = hint.get("message") or "최신/실시간 정보는 웹 검색을 이용해 주세요."
            _append_chat_history(request, q_in, msg)
            return _ok(
                {
                    "mode": "hint_web",
                    "code": hint.get("code") or "HINT_WEB",
                    "answer_text": msg,
                    "answer": msg,
                    "answer_html": "",
                    "hits": [],
                    "sources": [],
                    "model": _env_model_rag(),
                    "session_id": session_id,
                    "domain": domain,
                    "risk": risk,
                }
            )

    # 6) FAQ 먼저(비용/품질 최적화 + 기존 흐름 유지)
    try:
        faq_answer = find_best_faq_answer(q)
    except Exception:
        faq_answer = None

    if faq_answer:
        ans = _conservative_wrap(faq_answer, domain=domain, risk=risk) if conservative else faq_answer

        # 로그/히스토리
        try:
            log_chat_message(
                request=request,
                session_id=session_id,
                channel="qarag",
                mode="faq",
                role="user",
                message_type="query",
                question=q_in,
                content=q_in,
                sources=[],
                meta_extra={"where": "qa_policy_view", "stage": "faq"},
            )
            log_chat_message(
                request=request,
                session_id=session_id,
                channel="qarag",
                mode="faq",
                role="assistant",
                message_type="answer",
                question=q_in,
                content=ans,
                answer_excerpt=ans[:500],
                sources=[],
                meta_extra={"where": "qa_policy_view", "stage": "faq"},
            )
        except Exception:
            pass

        hist = request.session.get("chat_history", [])
        hist.append({"q": q_in, "a": ans})
        request.session["chat_history"] = hist
        request.session.modified = True

        return _ok(
            {
                "mode": "faq",
                "msg": "FAQ 답변",
                "model": _env_model_rag(),
                "answer_text": ans,
                "answer": ans,
                "answer_html": "",
                "hits": [],
                "sources": [],
                "session_id": session_id,
                "domain": domain,
                "risk": risk,
            }
        )

    # 7) RAG 먼저 시도 → 근거 있으면 RAG 최종, 없으면 WEB
    rag_text = ""
    hits_payload_ui: List[Dict[str, Any]] = []
    hits_payload_raw: List[Dict[str, Any]] = []
    hits_payload_quality: List[Dict[str, Any]] = []

    allowed_rag = False
    limit_rag = 0
    used_rag = 0

    if not force_web:
        # 7-1) RAG quota
        try:
            allowed_rag, limit_rag, used_rag = check_and_increment_usage(request, "rag")
        except Exception:
            allowed_rag, limit_rag, used_rag = True, 0, 0

    if allowed_rag:
        try:
            history_list = request.session.get("chat_history", [])

            topk = max(1, int(getattr(settings, "RAG_QUERY_TOPK", 5)))
            fallback_topk = max(topk + 5, int(getattr(settings, "RAG_FALLBACK_TOPK", 12)))
            max_sources = int(getattr(settings, "RAG_MAX_SOURCES", 8))

            res = rag_answer_grounded_with_history(
                q,
                history_list,
                base_retriever_func=rag_answer_grounded,
                initial_topk=topk,
                fallback_topk=fallback_topk,
                max_sources=max_sources,
            )

            if isinstance(res, tuple) and len(res) >= 2:
                rag_text, used_hits = res[0], res[1]
            elif isinstance(res, dict):
                rag_text = (res.get("answer") or res.get("text") or "")
                used_hits = (res.get("hits") or res.get("sources") or [])
            else:
                rag_text = str(res)
                used_hits = []

            # hits 정규화
            hits_payload_raw = []
            for i, h in enumerate(used_hits or [], start=1):
                if isinstance(h, dict):
                    m = h.get("meta") or {}
                    if not isinstance(m, dict):
                        m = {}

                    try:
                        citation_idx = int(
                            h.get("citation_idx")
                            or h.get("idx")
                            or m.get("citation_idx")
                            or m.get("idx")
                            or i
                        )
                    except (TypeError, ValueError):
                        citation_idx = i

                    hits_payload_raw.append(
                        {
                            # 답변 생성 당시 번호를 보존해야 [4]와 근거 #4가 연결된다.
                            "idx": citation_idx,
                            "citation_idx": citation_idx,
                            "title": m.get("title") or m.get("url") or h.get("title") or h.get("url") or f"문서 {i}",
                            "source": m.get("source_name") or m.get("source") or h.get("source") or "",
                            "url": m.get("url") or h.get("url") or "",
                            "snippet": (_hit_text(h) or "")[:800],
                            "score": (m.get("score") if "score" in m else h.get("score")),
                        }
                    )
                else:
                    hits_payload_raw.append(
                        {
                            "idx": i,
                            "citation_idx": i,
                            "title": str(h),
                            "source": "",
                            "url": "",
                            "snippet": "",
                            "score": None,
                        }
                    )

            # 묶음 인용([1, 2, 5])까지 포함해 실제 인용된 카드만 UI에 전달한다.
            # 검색 hit가 없다면 답변에 남은 인용 표기도 함께 제거한다.
            rag_text = _fix_invalid_citations(rag_text, hits_payload_raw)
            hits_payload_ui = _filter_hits_used_in_answer(
                rag_text,
                hits_payload_raw,
                fallback_count=1,
            )

            ui_max_cards = int(getattr(settings, "RAG_EVIDENCE_MAX_CARDS", 5))
            ui_min_score = getattr(settings, "RAG_EVIDENCE_MIN_SCORE", None)
            try:
                ui_min_score = float(ui_min_score) if ui_min_score is not None else None
            except Exception:
                ui_min_score = None

            # 품질 필터 결과는 RAG 채택 여부 판단에만 사용한다. 이미 답변에 실제
            # 인용된 카드를 다시 잘라내면 본문의 번호와 근거 패널이 어긋난다.
            hits_payload_quality = filter_source_cards_dicts(
                hits_payload_ui,
                max_cards=ui_max_cards,
                min_score=ui_min_score,
                drop_boilerplate=True,
                dedupe=True,
            )

        except Exception as e:
            log.exception("qa_policy_view RAG 실패: %s", e)
            rag_text = ""
            hits_payload_ui = []
            hits_payload_raw = []
            hits_payload_quality = []
    else:
        # ✅ rag 강제면 web로 넘기지 말고 여기서 종료
        if force_rag:
            return _fail(
                "오늘 사용할 수 있는 RAG 횟수를 모두 사용했습니다.",
                extra={"code": "limit_exceeded", "kind": "rag", "limit": limit_rag, "used": used_rag},
                status_code=429,
            )

    # 7-2) 근거 OK면 RAG 최종
    if (not force_web) and rag_text and _evidence_ok(hits_payload_quality):
        ans = _conservative_wrap(rag_text, domain=domain, risk=risk) if conservative else rag_text
        normalized_sources = _normalize_rag_sources(hits_payload_ui)

        try:
            log_chat_message(
                request=request,
                session_id=session_id,
                channel="qarag",
                mode="rag",
                role="user",
                message_type="query",
                question=q_in,
                content=q_in,
                sources=[],
                meta_extra={"where": "qa_policy_view", "stage": "rag_ok", "domain": domain, "risk": risk},
            )
            log_chat_message(
                request=request,
                session_id=session_id,
                channel="qarag",
                mode=("conservative" if conservative else "rag"),
                role="assistant",
                message_type="answer",
                question=q_in,
                content=ans,
                answer_excerpt=ans[:500],
                sources=hits_payload_raw,
                meta_extra={"where": "qa_policy_view", "stage": "rag_ok", "domain": domain, "risk": risk},
            )
        except Exception:
            pass

        hist = request.session.get("chat_history", [])
        hist.append({"q": q_in, "a": ans})
        request.session["chat_history"] = hist
        request.session.modified = True

        return _ok(
            {
                "mode": ("conservative" if conservative else "rag"),
                "msg": "RAG 답변",
                "model": _env_model_rag(),
                "answer_text": ans or "(빈 응답)",
                "answer": ans or "(빈 응답)",
                "answer_html": "",
                "hits": hits_payload_ui,
                "sources": hits_payload_ui,
                "sources_norm": normalized_sources,
                "session_id": session_id,
                "domain": domain,
                "risk": risk,
            }
        )

    # ✅ force_rag 인데 근거가 약해도 답은 주고 싶다면(기존 로직 유지)
    if (not force_web) and force_rag and (rag_text or "").strip():
        ans = _conservative_wrap(rag_text, domain=domain, risk=risk) if conservative else rag_text
        normalized_sources = _normalize_rag_sources(hits_payload_ui)

        hist = request.session.get("chat_history", [])
        hist.append({"q": q_in, "a": ans})
        request.session["chat_history"] = hist
        request.session.modified = True

        return _ok(
            {
                "mode": ("conservative" if conservative else "rag_weak"),
                "msg": "RAG 강제(근거 부족)",
                "model": _env_model_rag(),
                "answer_text": ans or "(빈 응답)",
                "answer": ans or "(빈 응답)",
                "answer_html": "",
                "hits": hits_payload_ui,
                "sources": hits_payload_ui,
                "sources_norm": normalized_sources,
                "session_id": session_id,
                "domain": domain,
                "risk": risk,
            }
        )

    # 7-3) 근거 부족 → WEB
    # 루머/사생활 성격이면 WEB 확장은 피하는 편이 안전하니, 보수 안내로 마무리
    if conservative and (not force_rag):
        ans = _conservative_wrap(
            "현재 내부 근거만으로는 단정할 수 없어서, 공개적으로 검증 가능한 자료(공식 발표/신뢰도 높은 보도/원문 문서)를 기준으로 확인하는 게 좋아.",
            domain=domain,
            risk=risk,
        )
        return _ok(
            {
                "mode": "conservative",
                "msg": "보수 응답",
                "model": _env_model_direct(),
                "answer_text": ans,
                "answer": ans,
                "hits": [],
                "sources": [],
                "session_id": session_id,
                "domain": domain,
                "risk": risk,
            }
        )

    if force_rag and not (rag_text or "").strip():
        msg = "내부 근거(RAG)만으로는 지금 답을 만들기 어려워. 질문을 더 구체화하거나 관련 문서를 추가해줘."
        ans = _conservative_wrap(msg, domain=domain, risk=risk) if conservative else msg

        hist = request.session.get("chat_history", [])
        hist.append({"q": q_in, "a": ans})
        request.session["chat_history"] = hist
        request.session.modified = True

        return _ok(
            {
                "mode": "rag_only",
                "msg": "RAG 강제(결과 없음)",
                "model": _env_model_rag(),
                "answer_text": ans,
                "answer": ans,
                "answer_html": "",
                "hits": hits_payload_ui,
                "sources": hits_payload_ui,
                "session_id": session_id,
                "domain": domain,
                "risk": risk,
            }
        )

    # web quota
    try:
        allowed_web, limit_web, used_web = check_and_increment_usage(request, "web")
    except Exception:
        allowed_web, limit_web, used_web = True, 0, 0

    if not allowed_web:
        msg = (
            "내부 근거(RAG)가 부족해서 웹 검색으로 전환하려 했어.\n"
            "그런데 오늘 웹 검색 사용량을 초과해서 여기서 답변이 종료됐어."
        )
        ans = _conservative_wrap(msg, domain=domain, risk=risk) if conservative else msg

        hist = request.session.get("chat_history", [])
        hist.append({"q": q_in, "a": ans})
        request.session["chat_history"] = hist
        request.session.modified = True

        return _ok(
            {
                "mode": "web_quota_exceeded",
                "code": "limit_exceeded",
                "kind": "web",
                "limit": limit_web,
                "used": used_web,
                "model": _env_model_direct(),
                "answer_text": ans,
                "answer": ans,
                "answer_html": "",
                "hits": [],
                "sources": [],
                "session_id": session_id,
                "domain": domain,
                "risk": risk,
            }
        )

    try:
        time_ctx = _build_time_ctx()
        web_text, headlines = _unpack_answer_sources(gemini_answer_with_news(q, ctx=time_ctx))

        srcs: List[Dict[str, Any]] = []
        for h in (headlines or []):
            if isinstance(h, dict):
                srcs.append(
                    {
                        "title": (h.get("title") or h.get("url") or "(제목 없음)"),
                        "url": (h.get("url") or ""),
                        "snippet": (h.get("snippet") or h.get("summary") or ""),
                        "source": (h.get("source") or ""),
                    }
                )
            else:
                srcs.append({"title": str(h), "url": "", "snippet": "", "source": ""})

        ans = web_text or ""

        try:
            log_chat_message(
                request=request,
                session_id=session_id,
                channel="qarag",
                mode="web",
                role="user",
                message_type="query",
                question=q_in,
                content=q_in,
                sources=[],
                meta_extra={"where": "qa_policy_view", "stage": "web_fallback", "domain": domain, "risk": risk},
            )
            log_chat_message(
                request=request,
                session_id=session_id,
                channel="qarag",
                mode="web",
                role="assistant",
                message_type="answer",
                question=q_in,
                content=ans,
                answer_excerpt=ans[:500],
                sources=srcs,
                meta_extra={"where": "qa_policy_view", "stage": "web_fallback", "domain": domain, "risk": risk},
            )
        except Exception:
            pass

        hist = request.session.get("chat_history", [])
        hist.append({"q": q_in, "a": ans})
        request.session["chat_history"] = hist
        request.session.modified = True

        return _ok(
            {
                "mode": "web",
                "msg": "웹 답변",
                "model": _env_model_direct(),
                "answer_text": ans,
                "answer": ans,
                "hits": [],
                "sources": srcs,
                "session_id": session_id,
                "domain": domain,
                "risk": risk,
            }
        )

    except Exception as e:
        log.exception("qa_policy_view WEB 실패: %s", e)
        return _fail(f"웹 QA 오류: {e}")
