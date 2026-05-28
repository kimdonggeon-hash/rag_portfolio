# ragapp/news_views/news_views.py
from __future__ import annotations

import os
import json
import secrets
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any
from zoneinfo import ZoneInfo

from django.shortcuts import render
from django.http import JsonResponse, HttpRequest, HttpResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.contrib.admin.views.decorators import staff_member_required
from django.utils import timezone
from django.conf import settings

from ragapp.utils.pii_guard import detect_pii, redact_pii
from ragapp.utils.ref_links import resolve_final_url
from ragapp.services.usage_limiter import check_and_increment_usage
from ragapp.machine.web_safety_guard import is_web_safety_blocked, web_blocked_json



# 업로드 화면 전용 뷰는 분리된 모듈에서 임포트 (URL에서 이 심볼을 직접 쓰는 경우가 많아서 유지)
from .upload_views import upload_doc_view  # noqa: F401

# Feedback 모델이 없을 수 있으므로 안전 가드
try:
    from ragapp.models import ChatQueryLog, Feedback  # type: ignore
except Exception:  # pragma: no cover
    from ragapp.models import ChatQueryLog  # type: ignore
    Feedback = None  # type: ignore

from ragapp.services.safety import is_sensitive_question, safe_block_response
from ragapp.services.domain_router import decide_domain
from ragapp.services.web_hint_router import decide_web_hint
from ragapp.services.source_quality import filter_source_cards_dicts
from ragapp.services.utils import client_ip_for_log
from ragapp.qa_data import find_best_faq_answer

# ✅ Legal 공통은 utils/legal.py 한 군데서만
from ragapp.utils.legal import validate_required_consents, build_legal_context

# 서비스 레이어
from ragapp.services.news_services import (
    gemini_answer_with_news,
    rag_answer_grounded,
    rag_answer_grounded_with_history,
)

from ragapp.log_utils import log_success, log_error

log = logging.getLogger(__name__)

_DOW_KR = ["월", "화", "수", "목", "금", "토", "일"]

def _time_ctx_kst() -> dict:
    """
    gemini_answer_with_news(ctx=...)에 넘길 KST 기준시각 컨텍스트 생성
    """
    try:
        if ZoneInfo is not None:
            kst = ZoneInfo("Asia/Seoul")
            now = timezone.now().astimezone(kst)
        else:
            now = timezone.localtime()
    except Exception:
        now = timezone.localtime()

    return {
        "today_iso": now.date().isoformat(),
        "today_kr": now.strftime("%Y년 %m월 %d일"),
        "dow_kr": _DOW_KR[now.weekday()],
        "now_kst_iso": now.isoformat(timespec="seconds"),
    }

def _web_msg_from_sources(srcs: list) -> str:
    """
    RSS 참고자료가 있으면 '웹 검색 완료'
    없으면 '참고자료 없음(직답)' 으로 UI msg를 구분
    """
    return "웹 검색 완료" if (srcs and len(srcs) > 0) else "참고자료 없음(직답)"


def _normalize_rag_sources(raw_sources: Any) -> List[Dict[str, Any]]:
    """
    템플릿(card_rag.html)에서 바로 쓸 수 있게 rag_sources 형태 통일.
    반환:
      [{"title": "...", "url": "...", "chunk": "...", "score": 0.87}, ...]
    """
    norm: List[Dict[str, Any]] = []
    if not raw_sources:
        return norm

    for i, s in enumerate(raw_sources):
        if isinstance(s, dict):
            title = (
                s.get("title")
                or s.get("page_title")
                or s.get("file_name")
                or s.get("id")
                or f"근거 {i + 1}"
            )
            url = s.get("url") or s.get("link") or ""
            chunk = (
                s.get("chunk")
                or s.get("snippet")
                or s.get("text")
                or s.get("page_content")
                or ""
            )
            score = s.get("score") or s.get("_score") or s.get("similarity")
        else:
            title = str(s)
            url = ""
            chunk = ""
            score = None

        norm.append({"title": title, "url": url, "chunk": chunk, "score": score})

    return norm

def _hit_text(h: dict) -> str:
    """retriever가 어떤 키로 텍스트를 주든 근거 텍스트를 안전하게 뽑는다."""
    if not isinstance(h, dict):
        return ""
    m = h.get("meta") or {}
    if not isinstance(m, dict):
        m = {}

    for k in ("snippet", "chunk", "text", "page_content", "content", "document"):
        v = h.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    for k in ("snippet", "chunk", "text", "page_content", "content", "document"):
        v = m.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    return ""

def _pii_block_msg(kind: str | None) -> str:
    k = kind or "개인정보"
    return (
        f"입력 내용에 개인정보로 보이는 정보({k})가 포함되어 요청을 처리하지 않았습니다. "
        "전화번호/주민번호/주소/이메일 등은 제거하거나 가명 처리 후 다시 시도해 주세요."
    )


def _guard_pii_or_none(q: str, *, fail_closed: bool | None = None) -> tuple[bool, str | None]:
    """
    returns: (blocked, kind)
    - blocked=True면 개인정보 포함(or 가드 오류로 안전 차단)
    - kind는 'phone'/'rrn'/'address'/'email' ... 또는 'pii_guard_error'
    - fail_closed:
        * True  -> detect_pii 예외 시 안전 차단
        * False -> detect_pii 예외 시 통과
        * None  -> settings/env로 결정 (기본값)
    """
    if fail_closed is None:
        # ✅ 기본은 안전하게: 운영에서 True 권장
        # env: PII_GUARD_FAIL_CLOSED=1 / settings.PII_GUARD_FAIL_CLOSED=True
        try:
            v = os.environ.get("PII_GUARD_FAIL_CLOSED", None)
            if v is None:
                v = getattr(settings, "PII_GUARD_FAIL_CLOSED", True)
            fail_closed = str(v).strip().lower() in ("1", "true", "yes", "y", "on")
        except Exception:
            fail_closed = True

    try:
        hit = detect_pii(q)

        if isinstance(hit, bool):
            return hit, None

        if isinstance(hit, (tuple, list)) and hit:
            blocked = bool(hit[0])
            kind = None
            if len(hit) >= 2 and hit[1] is not None:
                kind = str(hit[1])
            return blocked, kind

        if isinstance(hit, dict):
            blocked = bool(hit.get("hit") or hit.get("blocked") or hit.get("is_hit"))
            kind = hit.get("kind") or hit.get("type") or hit.get("pii_kind")
            return blocked, (str(kind) if kind else None)

        blocked = bool(
            getattr(hit, "hit", False)
            or getattr(hit, "blocked", False)
            or getattr(hit, "is_hit", False)
        )
        kind = getattr(hit, "kind", None) or getattr(hit, "type", None) or getattr(hit, "pii_kind", None)
        return blocked, (str(kind) if kind else None)

    except Exception as e:
        # ✅ 장애 원인은 서버 로그로 남기고, 정책에 따라 차단/통과
        log.exception("PII guard error: %s", e)
        if fail_closed:
            return True, "pii_guard_error"
        return False, None


def _render_home_ctx(request: HttpRequest, web_state: dict, rag_state: dict) -> HttpResponse:
    """home() 중간에 조기 리턴할 때 ctx 구성 중복 제거용"""
    try:
        web_sources_json = json.dumps(web_state.get("sources", []), ensure_ascii=False)
    except Exception:
        web_sources_json = "[]"

    ctx = {
        "web_query": web_state.get("query", ""),
        "web_answer": web_state.get("answer", ""),
        "web_sources": web_state.get("sources", []),
        "web_sources_json": web_sources_json,
        "web_error": web_state.get("error", None),
        "web_msg": web_state.get("msg", None),
        "web_log_id": web_state.get("log_id", None),
        "rag_query": rag_state.get("query", ""),
        "rag_answer": rag_state.get("answer", ""),
        "rag_chunks": [],
        "rag_error": rag_state.get("error", None),
        "rag_msg": rag_state.get("msg", None),
        "rag_sources": rag_state.get("sources", []),
        "rag_log_id": rag_state.get("log_id", None),
        "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
        "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),
        "VECTOR_DB_PATH": _vector_db_path(),
        "model_name_gemini": _env_model_direct(),
        "model_name_rag": _env_model_rag(),
    }
    ctx.update(_api_paths_ctx())
    ctx.update(_compat_aliases_web(web_state, rag_state))
    ctx.update(build_legal_context())
    return render(request, "ragapp/news.html", ctx)


# ─────────────────────────────────────────────
# ★ 모델 표시명은 무조건 .env에서만 읽기 (없으면 즉시 에러)
# ─────────────────────────────────────────────
def _env_flag(name: str, default: bool = False) -> bool:
    """
    env 또는 settings에 name이 있으면 truthy로 해석.
    예: ALLOW_MISSING_MODEL_ENV=1 / True / on / yes
    """
    v = os.environ.get(name, None)
    if v is None:
        v = getattr(settings, name, None)

    if v is None:
        return default

    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")

def _require_env(keys: tuple[str, ...], label: str) -> str:
    for k in keys:
        v = os.environ.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # ✅ (옵션) 개발/운영 임시: 모델 env 누락이어도 500 대신 폴백 문자열
    if _env_flag("ALLOW_MISSING_MODEL_ENV", default=False):
        return f"{label} (not configured)"

    raise RuntimeError(
        f"{label} 모델명이 .env에 없습니다. 다음 키 중 하나를 .env에 설정하세요: {', '.join(keys)}"
    )


def _env_model_direct() -> str:
    return _require_env(
        (
            "GEMINI_MODEL_DIRECT",
            "GEMINI_TEXT_MODEL",
            "VERTEX_TEXT_MODEL",
            "GEMINI_MODEL",
            "GEMINI_MODEL_DEFAULT",
        ),
        label="웹/Gemini",
    )


def _env_model_rag() -> str:
    return _require_env(
        (
            "GEMINI_MODEL_RAG",
            "GEMINI_TEXT_MODEL",
            "VERTEX_TEXT_MODEL",
            "GEMINI_MODEL",
            "GEMINI_MODEL_DEFAULT",
        ),
        label="RAG",
    )


# ─────────────────────────────────────────────
# 공용 JSON 응답
# ─────────────────────────────────────────────
def _ok(d: dict) -> JsonResponse:
    d.setdefault("ok", True)
    d.setdefault("guard_hit", False)
    d.setdefault("guard_reason", "")
    return JsonResponse(d, status=200, json_dumps_params={"ensure_ascii": False})

def _fail(msg: str, extra: dict | None = None, status_code: int = 400) -> JsonResponse:
    p = {
        "ok": False,
        "error": msg,
        "message": msg,
        "guard_hit": False,
        "guard_reason": "",
    }
    if extra:
        p.update(extra)
    return JsonResponse(p, status=status_code, json_dumps_params={"ensure_ascii": False})

# ─────────────────────────────────────────────
# 설정: 메타-전용 인덱싱
# ─────────────────────────────────────────────
_WEB_INGEST_META_ONLY = getattr(settings, "WEB_INGEST_META_ONLY", None)
if _WEB_INGEST_META_ONLY is None:
    _WEB_INGEST_META_ONLY = not bool(getattr(settings, "ALLOW_STORE_NEWS_BODY", False))


# 레이트리밋(세션)
def _ratelimit(request: HttpRequest, key: str, seconds: int) -> bool:
    now = timezone.now()
    last = request.session.get(key)

    if last:
        last_dt = None
        try:
            last_dt = datetime.fromisoformat(last)
            # ✅ naive면 현재 TZ로 aware 변환
            if timezone.is_naive(last_dt):
                last_dt = timezone.make_aware(last_dt, timezone.get_current_timezone())
        except Exception:
            last_dt = None

        if last_dt:
            try:
                if (now - last_dt).total_seconds() < seconds:
                    return False
            except Exception:
                # tz 꼬임 등 예외가 나도 레이트리밋 자체는 계속 동작하게
                pass

    request.session[key] = now.isoformat()
    request.session.modified = True
    return True


def _truthy(v) -> bool:
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ("on", "1", "true", "yes", "y")


def _consent_ok_server(request: HttpRequest) -> bool:
    if (
        request.session.get("consent_ok") is True
        and request.session.get("consent_stored_to") in ("ConsentEvidence", "MyLog")
    ):
        return True
    return False


# 어떤 형태로 리턴돼도 안전 언패킹
def _unpack_answer_sources(res) -> tuple[str, list]:
    ans = ""
    srcs: list = []
    if res is None:
        return ans, srcs
    if isinstance(res, tuple):
        if len(res) >= 1 and isinstance(res[0], str):
            ans = res[0]
        if len(res) >= 2 and isinstance(res[1], (list, tuple)):
            srcs = list(res[1])
        return ans or "", srcs or []
    if isinstance(res, dict):
        ans = str(res.get("answer", "") or res.get("text", "") or "")
        raw = res.get("sources") or res.get("headlines") or []
        if isinstance(raw, (list, tuple)):
            srcs = list(raw)
        return ans or "", srcs or []
    if isinstance(res, str):
        return res, []
    try:
        return str(res), []
    except Exception:
        return "", []


# ─────────────────────────────────────────────
# ✅ 현재 사용 중인 벡터 DB 경로 (SQLite)
# ─────────────────────────────────────────────
def _vector_db_path() -> str:
    """
    현재 사용하는 벡터 SQLite DB 경로를 문자열로 반환.
    - settings.VECTOR_DB_PATH 우선
    - 없으면 환경변수 VECTOR_DB_PATH
    - 그래도 없으면 BASE_DIR/sqlite3/vector_store.sqlite3
    """
    try:
        p = getattr(settings, "VECTOR_DB_PATH", None)
    except Exception:
        p = None

    p = p or os.environ.get("VECTOR_DB_PATH")
    if p:
        return str(p)

    base = getattr(settings, "BASE_DIR", Path.cwd())
    return str(Path(base) / "sqlite3" / "vector_store.sqlite3")


def _api_paths_ctx() -> dict:
    return {
        "WEB_API_PATH": (os.environ.get("WEB_API_PATH") or getattr(settings, "WEB_API_PATH", "/api/web_qa")),
        "RAG_API_PATH": (os.environ.get("RAG_API_PATH") or getattr(settings, "RAG_API_PATH", "/api/rag_qa")),
        "QA_POLICY_API_PATH": (os.environ.get("QA_POLICY_API_PATH") or getattr(settings, "QA_POLICY_API_PATH", "/api/qa_policy")),
    }


def _compat_aliases_web(web_state: dict, rag_state: dict) -> dict:
    def _srcs(slist):
        out = []
        for s in (slist or []):
            if isinstance(s, dict):
                out.append(
                    {
                        "title": s.get("title", ""),
                        "url": s.get("url", ""),
                        "source": s.get("source", ""),
                        "snippet": s.get("snippet", ""),
                    }
                )
            else:
                out.append({"title": str(s), "url": "", "source": "", "snippet": ""})
        return out

    return {
        "q_gemini": web_state.get("query", ""),
        "gemini_answer": web_state.get("answer", ""),
        "gemini_error": web_state.get("error", ""),
        "news_list": _srcs(web_state.get("sources", [])),
        "q_rag": rag_state.get("query", ""),
        "rag_sources": rag_state.get("sources", []),
    }


def _has_field(model, name: str) -> bool:
    try:
        return any(getattr(f, "name", None) == name for f in model._meta.get_fields())
    except Exception:
        return False


def _create_simple_log(
    *,
    request: HttpRequest,
    mode: str,
    question: str,
    answer_excerpt: str,
    is_error: bool,
    error_msg: str,
    sources: list | None = None,
    meta: dict | None = None,
) -> ChatQueryLog:
    now = timezone.now()
    client_ip = client_ip_for_log(request)

    # ✅ 필드 존재하는 것만 채운다 (스키마 차이로 500 방지)
    values = {
        "mode": mode,
        "question": question,
        "answer_excerpt": (answer_excerpt or "")[:500],
        "client_ip": client_ip,
        "created_at": now,
        "is_error": bool(is_error),
        "error_msg": (error_msg or ""),
        "feedback": "",
        "was_helpful": None,
    }

    kwargs: dict = {}
    for k, v in values.items():
        if _has_field(ChatQueryLog, k):
            kwargs[k] = v

    # optional fields
    if _has_field(ChatQueryLog, "session_id"):
        kwargs["session_id"] = get_chat_session_id(request)
    if _has_field(ChatQueryLog, "channel"):
        kwargs["channel"] = "home"
    if _has_field(ChatQueryLog, "sources") and sources is not None:
        kwargs["sources"] = sources
    if _has_field(ChatQueryLog, "meta") and meta is not None:
        kwargs["meta"] = meta

    if not kwargs:
        raise RuntimeError("ChatQueryLog에 저장 가능한 필드가 없습니다. (스키마 확인 필요)")

    return ChatQueryLog.objects.create(**kwargs)


def _quota_consume_for_home(request: HttpRequest, kind: str) -> tuple[bool, int, int, str | None]:
    """
    home() 패널용: JSON을 리턴하지 말고, 상태로 에러를 내려야 함.
    반환: (allowed, limit, used, err_msg)
    """
    try:
        allowed, limit, used = check_and_increment_usage(request, kind)
        return bool(allowed), int(limit or 0), int(used or 0), None
    except Exception as e:
        log.exception("usage limiter(%s) 실패: %s", kind, e)
        return False, 0, 0, "사용량 체크 오류로 요청을 처리할 수 없습니다. (서버 로그 확인)"


# ─────────────────────────────────────────────
# 메인 홈 (웹/Gemini + RAG 패널)
# ─────────────────────────────────────────────
@require_http_methods(["GET", "POST"])
@ensure_csrf_cookie
def home(request: HttpRequest):
    def get_web_state():
        st = request.session.get("web_state", {})
        return {
            "query": st.get("query", ""),
            "answer": st.get("answer", ""),
            "sources": st.get("sources", []),
            "msg": st.get("msg", None),
            "error": st.get("error", None),
            "log_id": st.get("log_id", None),
        }

    def get_rag_state():
        st = request.session.get("rag_state", {})
        return {
            "query": st.get("query", ""),
            "answer": st.get("answer", ""),
            "sources": st.get("sources", []),
            "msg": st.get("msg", None),
            "error": st.get("error", None),
            "log_id": st.get("log_id", None),
        }

    def _trim_text(v: Any, max_len: int) -> str:
        s = "" if v is None else str(v)
        s = s.strip()
        return s if len(s) <= max_len else (s[: max_len - 1] + "…")

    def _cap_sources_for_session(sources: Any, *, max_items: int = 8, max_text: int = 500) -> list[dict]:
        out: list[dict] = []
        if not isinstance(sources, (list, tuple)):
            return out
        for s in list(sources)[:max_items]:
            if isinstance(s, dict):
                out.append(
                    {
                        "title": _trim_text(s.get("title", ""), 160),
                        "url": _trim_text(s.get("url", ""), 500),
                        "source": _trim_text(s.get("source", ""), 80),
                        "snippet": _trim_text(s.get("snippet") or s.get("chunk") or "", max_text),
                        "score": s.get("score", None),
                    }
                )
            else:
                out.append({"title": _trim_text(s, 160), "url": "", "source": "", "snippet": "", "score": None})
        return out

    def save_web_state(new_state):
        new_state = dict(new_state or {})
        new_state["sources"] = _cap_sources_for_session(new_state.get("sources", []), max_items=8, max_text=400)
        request.session["web_state"] = new_state
        request.session.modified = True

    def save_rag_state(new_state):
        new_state = dict(new_state or {})
        new_state["sources"] = _cap_sources_for_session(new_state.get("sources", []), max_items=8, max_text=500)
        request.session["rag_state"] = new_state
        request.session.modified = True

    # 첫 진입(GET, 쿼리스트링 없음)이면 세션 초기화
    if request.method == "GET" and not request.GET:
        request.session.pop("web_state", None)
        request.session.pop("rag_state", None)
        web_state = {"query": "", "answer": "", "sources": [], "msg": None, "error": None, "log_id": None}
        rag_state = {"query": "", "answer": "", "sources": [], "msg": None, "error": None, "log_id": None}
    else:
        web_state = get_web_state()
        rag_state = get_rag_state()

        if request.method == "POST":
            action = (request.POST.get("action") or request.POST.get("act") or "").strip()
            if not action:
                if (request.POST.get("query_web") or "").strip():
                    action = "web_search"
                elif (request.POST.get("query_rag") or "").strip():
                    action = "rag_search"

            # ── 웹 인덱싱 시 동의 체크 ────────────────────────
            if action == "web_ingest":
                if _consent_ok_server(request):
                    request.session["consent_ok"] = True
                    request.session.modified = True
                else:
                    ok_consent, err_consent = validate_required_consents(request)
                    if not ok_consent:
                        web_state["error"] = err_consent
                        save_web_state(web_state)
                        save_rag_state(rag_state)
                        return _render_home_ctx(request, web_state, rag_state)

            # ── 웹 검색 ───────────────────────────────────────
            if action == "web_search":
                q = (request.POST.get("query_web") or "").strip()
                if not q:
                    web_state = {
                        "query": "",
                        "answer": "",
                        "sources": [],
                        "msg": None,
                        "error": "검색어를 입력해 주세요.",
                        "log_id": None,
                    }
                else:
                    # ✅ PII 먼저 차단 (quota/외부호출/로그 전에)
                    blocked, kind = _guard_pii_or_none(q, fail_closed=True)
                    if blocked:
                        web_state = {
                            "query": redact_pii(q),
                            "answer": "",
                            "sources": [],
                            "msg": None,
                            "error": _pii_block_msg(kind),
                            "log_id": None,
                        }
                    else:
                        # ✅ web safety guard (quota/외부호출/로그 전에)
                        if is_web_safety_blocked(q):
                            web_state = {
                                "query": q,
                                "answer": "이 요청에 대해서는 제공 할 수 가 없습니다",
                                "sources": [],
                                "msg": "차단",
                                "error": None,
                                "log_id": None,
                            }
                        else:
                            allowed, limit, used, err = _quota_consume_for_home(request, "web")
                            if err:
                                web_state = {
                                    "query": q,
                                    "answer": web_state.get("answer", ""),
                                    "sources": web_state.get("sources", []),
                                    "msg": None,
                                    "error": err,
                                    "log_id": web_state.get("log_id"),
                                }
                            elif not allowed:
                                web_state = {
                                    "query": q,
                                    "answer": web_state.get("answer", ""),
                                    "sources": web_state.get("sources", []),
                                    "msg": None,
                                    "error": f"오늘 사용할 수 있는 웹 검색 횟수를 모두 사용했습니다. (limit={limit}, used={used})",
                                    "log_id": web_state.get("log_id"),
                                }
                            else:
                                try:
                                    ans_text, headlines = _unpack_answer_sources(
                                        gemini_answer_with_news(q, ctx=_time_ctx_kst())
                                    )
                                    srcs: list[dict] = []
                                    for h in (headlines or []):
                                        try:
                                            raw_url = (h.get("url") if isinstance(h, dict) else "") or ""
                                            clean_url = resolve_final_url(raw_url) if raw_url else ""

                                            title = (
                                                (h.get("title") if isinstance(h, dict) else "")
                                                or clean_url
                                                or raw_url
                                                or "(제목 없음)"
                                            )

                                            srcs.append(
                                                {
                                                    "title": title,
                                                    "url": clean_url or raw_url,
                                                    "snippet": (h.get("snippet") if isinstance(h, dict) else "")
                                                    or (h.get("summary") if isinstance(h, dict) else ""),
                                                    "source": (h.get("source") if isinstance(h, dict) else "") or "",
                                                    "raw_url": raw_url,
                                                }
                                            )
                                        except Exception:
                                            srcs.append({"title": str(h), "url": "", "snippet": "", "source": ""})

                                    srcs = [
                                        s
                                        for s in (srcs or [])
                                        if isinstance(s, dict)
                                        and (((s.get("title") or "").strip()) or ((s.get("url") or "").strip()))
                                    ]

                                    log_obj = _create_simple_log(
                                        request=request,
                                        mode="gemini",
                                        question=q,
                                        answer_excerpt=(ans_text or ""),
                                        is_error=False,
                                        error_msg="",
                                        sources=srcs if isinstance(srcs, list) else [],
                                        meta={"where": "home.web_search", "no_sources": (not srcs)},
                                    )

                                    web_state = {
                                        "query": q,
                                        "answer": ans_text or "",
                                        "sources": srcs,
                                        "msg": _web_msg_from_sources(srcs),
                                        "error": None,
                                        "log_id": getattr(log_obj, "id", None),
                                    }
                                except Exception as e:
                                    log.exception("web_search 실패")
                                    err_log = _create_simple_log(
                                        request=request,
                                        mode="gemini",
                                        question=q,
                                        answer_excerpt="",
                                        is_error=True,
                                        error_msg=str(e),
                                        sources=[],
                                        meta={"where": "home.web_search", "stage": "gemini_answer_with_news"},
                                    )
                                    web_state = {
                                        "query": q,
                                        "answer": web_state.get("answer", ""),
                                        "sources": web_state.get("sources", []),
                                        "msg": None,
                                        "error": f"웹 검색 중 오류: {e}",
                                        "log_id": getattr(err_log, "id", None),
                                    }

            # ── 웹 검색 결과 인덱싱 ───────────────────────────
            elif action == "web_ingest":
                if not _ratelimit(request, "rate_web_ingest", 5):
                    web_state["error"] = "요청이 너무 잦습니다. 잠시 후 다시 시도하세요."
                    save_web_state(web_state)
                    save_rag_state(rag_state)
                    return _render_home_ctx(request, web_state, rag_state)

                q = (request.POST.get("query_web") or "").strip()
                answer_payload = request.POST.get("web_answer_payload", "") or ""
                raw_sources = request.POST.get("web_sources_payload", "") or "[]"
                try:
                    src_list = json.loads(raw_sources)
                except Exception:
                    src_list = []

                # ✅ PII면 인덱싱도 금지 (임베딩/저장 전에)
                blocked, kind = _guard_pii_or_none(q, fail_closed=True)
                if blocked:
                    web_state = {
                        "query": redact_pii(q),
                        "answer": answer_payload,
                        "sources": src_list if isinstance(src_list, list) else [],
                        "msg": None,
                        "error": _pii_block_msg(kind),
                        "log_id": web_state.get("log_id"),
                    }
                else:
                    # ✅ web safety guard (저장/임베딩 전에) — 인덱싱도 금지
                    if is_web_safety_blocked(q):
                        web_state = {
                            "query": q,
                            "answer": answer_payload,
                            "sources": src_list if isinstance(src_list, list) else [],
                            "msg": "차단",
                            "error": "이 요청에 대해서는 제공 할 수 가 없습니다",
                            "log_id": web_state.get("log_id"),
                        }
                        save_web_state(web_state)
                        save_rag_state(rag_state)
                        return _render_home_ctx(request, web_state, rag_state)

                    try:
                        usable_src_list = [
                            s
                            for s in (src_list or [])
                            if isinstance(s, dict)
                            and (((s.get("title") or "").strip()) or ((s.get("url") or "").strip()))
                        ]

                        # ✅ 참고자료 없으면 인덱싱 금지 (직답 폴백/빈 RSS 케이스)
                        if not usable_src_list:
                            web_state = {
                                "query": q,
                                "answer": answer_payload,
                                "sources": src_list if isinstance(src_list, list) else [],
                                "msg": None,
                                "error": "참고자료가 없어 인덱싱하지 않았습니다. (RSS 결과가 비었거나 직답 폴백)",
                                "log_id": web_state.get("log_id"),
                            }
                            save_web_state(web_state)
                            save_rag_state(rag_state)
                            return _render_home_ctx(request, web_state, rag_state)

                        pseudo_news_list = []
                        for s in usable_src_list:
                            if isinstance(s, dict):
                                raw_url = (s.get("url", "") or "").strip()
                                final_url = resolve_final_url(raw_url) if raw_url else ""

                                pseudo_news_list.append(
                                    {
                                        "title": s.get("title", ""),
                                        "url": final_url or raw_url,
                                        "final_url": final_url or raw_url,
                                        "source": s.get("source", ""),
                                        "published_at": "",
                                        "snippet": s.get("snippet", ""),
                                        "news_body": ("" if _WEB_INGEST_META_ONLY else s.get("snippet", "")),
                                    }
                                )
                            else:
                                pseudo_news_list.append(
                                    {
                                        "title": str(s),
                                        "url": "",
                                        "source": "",
                                        "published_at": "",
                                        "snippet": "",
                                        "news_body": "",
                                    }
                                )

                        ingest_info = indexto_chroma_safe(q, answer_payload, pseudo_news_list)
                        log.info("web_ingest 완료: %s", ingest_info)

                        web_state = {
                            "query": q,
                            "answer": answer_payload,
                            "sources": src_list if isinstance(src_list, list) else [],
                            "msg": "웹 검색 결과 인덱싱 완료",
                            "error": None,
                            "log_id": web_state.get("log_id"),
                        }
                    except Exception as e:
                        log.exception("web_ingest 실패")
                        web_state = {
                            "query": q,
                            "answer": answer_payload,
                            "sources": src_list if isinstance(src_list, list) else [],
                            "msg": None,
                            "error": f"웹결과 인덱싱 실패: {e}",
                            "log_id": web_state.get("log_id"),
                        }

            # ── RAG 검색 ──────────────────────────────────────
            elif action == "rag_search":
                q = (request.POST.get("query_rag") or "").strip()
                if not q:
                    rag_state = {
                        "query": "",
                        "answer": "",
                        "sources": [],
                        "msg": None,
                        "error": "질문을 입력해 주세요.",
                        "log_id": None,
                    }
                else:
                    blocked, kind = _guard_pii_or_none(q, fail_closed=True)
                    if blocked:
                        rag_state = {
                            "query": redact_pii(q),
                            "answer": "",
                            "sources": [],
                            "msg": None,
                            "error": _pii_block_msg(kind),
                            "log_id": None,
                        }
                    else:
                        dd = decide_domain(q)
                        if dd and getattr(dd, "action", None) == "block":
                            msg = getattr(dd, "message", "") or "요청을 처리할 수 없습니다."
                            rag_state = {
                                "query": q,
                                "answer": msg,
                                "sources": [],
                                "msg": "차단",
                                "error": None,
                                "log_id": None,
                            }

                        elif dd and getattr(dd, "action", None) == "clarify":
                            ask = getattr(dd, "ask", "") or "질문을 조금만 더 구체적으로 알려주세요."
                            rag_state = {
                                "query": q,
                                "answer": ask,
                                "sources": [],
                                "msg": "추가 질문",
                                "error": None,
                                "log_id": None,
                            }

                        else:
                            hint = decide_web_hint(q)
                            if hint:
                                msg = hint["message"]
                                rag_state = {
                                    "query": q,
                                    "answer": msg,
                                    "sources": [],
                                    "msg": "웹검색 안내",
                                    "error": None,
                                    "log_id": None,
                                }
                            else:
                                allowed, limit, used, err = _quota_consume_for_home(request, "rag")
                                if err:
                                    rag_state = {
                                        "query": q,
                                        "answer": rag_state.get("answer", ""),
                                        "sources": rag_state.get("sources", []),
                                        "msg": None,
                                        "error": err,
                                        "log_id": rag_state.get("log_id"),
                                    }
                                elif not allowed:
                                    rag_state = {
                                        "query": q,
                                        "answer": rag_state.get("answer", ""),
                                        "sources": rag_state.get("sources", []),
                                        "msg": None,
                                        "error": f"오늘 사용할 수 있는 RAG 질문 횟수를 모두 사용했습니다. (limit={limit}, used={used})",
                                        "log_id": rag_state.get("log_id"),
                                    }
                                else:
                                    try:
                                        topk = max(1, int(getattr(settings, "RAG_QUERY_TOPK", 5)))
                                        fallback_topk = max(topk + 5, int(getattr(settings, "RAG_FALLBACK_TOPK", 12)))
                                        max_sources = int(getattr(settings, "RAG_MAX_SOURCES", 8))

                                        res = rag_answer_grounded(
                                            q,
                                            initial_topk=topk,
                                            fallback_topk=fallback_topk,
                                            max_sources=max_sources,
                                        )

                                        if isinstance(res, tuple) and len(res) >= 2:
                                            rag_answer_text, used_hits = res[0], res[1]
                                        elif isinstance(res, dict):
                                            rag_answer_text = (res.get("answer") or res.get("text") or "")
                                            used_hits = (res.get("hits") or res.get("sources") or [])
                                        else:
                                            rag_answer_text = str(res)
                                            used_hits = []

                                        hits_payload: list[Dict[str, Any]] = []
                                        for i, h in enumerate(used_hits or [], start=1):
                                            if isinstance(h, dict):
                                                meta = h.get("meta") or {}
                                                title = (
                                                    (meta.get("title") if isinstance(meta, dict) else None)
                                                    or (meta.get("url") if isinstance(meta, dict) else None)
                                                    or h.get("title")
                                                    or h.get("url")
                                                    or f"문서 {i}"
                                                )
                                                source = (
                                                    (meta.get("source_name") if isinstance(meta, dict) else None)
                                                    or (meta.get("source") if isinstance(meta, dict) else None)
                                                    or h.get("source")
                                                    or ""
                                                )
                                                url = (meta.get("url") if isinstance(meta, dict) else None) or h.get("url") or ""
                                                snippet = _hit_text(h)
                                                score = (
                                                    meta.get("score")
                                                    if (isinstance(meta, dict) and "score" in meta)
                                                    else h.get("score")
                                                )

                                                hits_payload.append(
                                                    {
                                                        "title": title,
                                                        "source": source,
                                                        "url": url,
                                                        "snippet": snippet,
                                                        "score": score,
                                                    }
                                                )
                                            else:
                                                hits_payload.append(
                                                    {"title": str(h), "source": "", "url": "", "snippet": "", "score": None}
                                                )

                                        ui_max_cards = int(getattr(settings, "RAG_EVIDENCE_MAX_CARDS", 5))
                                        ui_min_score = getattr(settings, "RAG_EVIDENCE_MIN_SCORE", None)
                                        try:
                                            ui_min_score = float(ui_min_score) if ui_min_score is not None else None
                                        except Exception:
                                            ui_min_score = None

                                        hits_payload_ui = filter_source_cards_dicts(
                                            hits_payload,
                                            max_cards=ui_max_cards,
                                            min_score=ui_min_score,
                                            drop_boilerplate=True,
                                            dedupe=True,
                                        )

                                        normalized_sources = _normalize_rag_sources(hits_payload_ui)

                                        log_obj = _create_simple_log(
                                            request=request,
                                            mode="rag",
                                            question=q,
                                            answer_excerpt=(rag_answer_text or ""),
                                            is_error=False,
                                            error_msg="",
                                            sources=hits_payload,
                                            meta={"where": "home.rag_search", "hit_count": len(used_hits or [])},
                                        )

                                        rag_state = {
                                            "query": q,
                                            "answer": rag_answer_text or "",
                                            "sources": normalized_sources,
                                            "msg": "RAG 검색 완료",
                                            "error": None,
                                            "log_id": getattr(log_obj, "id", None),
                                        }
                                    except Exception as e:
                                        log.exception("rag_search 실패")
                                        err_log = _create_simple_log(
                                            request=request,
                                            mode="rag",
                                            question=q,
                                            answer_excerpt="",
                                            is_error=True,
                                            error_msg=str(e),
                                            sources=[],
                                            meta={"where": "home.rag_search", "stage": "rag_answer_grounded"},
                                        )
                                        rag_state = {
                                            "query": q,
                                            "answer": rag_state.get("answer", ""),
                                            "sources": rag_state.get("sources", []),
                                            "msg": None,
                                            "error": f"RAG 검색 중 오류: {e}",
                                            "log_id": getattr(err_log, "id", None),
                                        }

            elif action == "rag_seed":
                q = (request.POST.get("query_rag") or "").strip()
                rag_state = {
                    "query": q,
                    "answer": rag_state.get("answer", ""),
                    "sources": rag_state.get("sources", []),
                    "msg": "시드 업서트 완료 (예시)",
                    "error": None,
                    "log_id": rag_state.get("log_id"),
                }

            elif action == "chroma_init":
                q = (request.POST.get("query_rag") or "").strip()
                rag_state = {
                    "query": q,
                    "answer": rag_state.get("answer", ""),
                    "sources": rag_state.get("sources", []),
                    "msg": "컬렉션 초기화 완료 (예시)",
                    "error": None,
                    "log_id": rag_state.get("log_id"),
                }

            else:
                if (request.POST.get("query_web") or "").strip():
                    web_state = {
                        "query": (request.POST.get("query_web") or "").strip(),
                        "answer": web_state.get("answer", ""),
                        "sources": web_state.get("sources", []),
                        "msg": None,
                        "error": "요청을 해석할 수 없습니다. (action=web_search 폴백 실패)",
                        "log_id": web_state.get("log_id"),
                    }
                elif (request.POST.get("query_rag") or "").strip():
                    rag_state = {
                        "query": (request.POST.get("query_rag") or "").strip(),
                        "answer": rag_state.get("answer", ""),
                        "sources": rag_state.get("sources", []),
                        "msg": None,
                        "error": "요청을 해석할 수 없습니다. (action=rag_search 폴백 실패)",
                        "log_id": rag_state.get("log_id"),
                    }

        save_web_state(web_state)
        save_rag_state(rag_state)

    try:
        web_sources_json = json.dumps(web_state["sources"], ensure_ascii=False)
    except Exception:
        web_sources_json = "[]"

    ctx = {
        "web_query": web_state["query"],
        "web_answer": web_state["answer"],
        "web_sources": web_state["sources"],
        "web_sources_json": web_sources_json,
        "web_error": web_state["error"],
        "web_msg": web_state["msg"],
        "web_log_id": web_state.get("log_id"),
        "rag_query": rag_state["query"],
        "rag_answer": rag_state["answer"],
        "rag_chunks": [],
        "rag_error": rag_state["error"],
        "rag_msg": rag_state["msg"],
        "rag_sources": rag_state["sources"],
        "rag_log_id": rag_state.get("log_id"),
        "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
        "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),
        "VECTOR_DB_PATH": _vector_db_path(),
        "model_name_gemini": _env_model_direct(),
        "model_name_rag": _env_model_rag(),
    }
    ctx.update(_api_paths_ctx())
    ctx.update(_compat_aliases_web(web_state, rag_state))
    ctx.update(build_legal_context())
    return render(request, "ragapp/news.html", ctx)


# ─────────────────────────────────────────────
# 예전 news 뷰 (호환용)
# ─────────────────────────────────────────────
def news(request: HttpRequest):
    if request.method == "GET" and not request.GET:
        request.session.pop("gemini_state", None)
        request.session.pop("web_state", None)
        request.session.pop("rag_state", None)
        request.session.pop("chat_history", None)
        ctx = {
            "model_name_gemini": _env_model_direct(),
            "model_name_rag": _env_model_rag(),
            "q_gemini": "",
            "gemini_answer": "",
            "gemini_error": "",
            "news_list": [],
            "ingest_result": "",
            "ingest_error": "",
            "q_rag": "",
            "rag_answer": "",
            "rag_error": "",
            "rag_sources": [],
            "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),
            "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
            "VECTOR_DB_PATH": _vector_db_path(),
        }
        ctx.update(_api_paths_ctx())
        ctx.update(build_legal_context())
        resp = render(request, "ragapp/news.html", ctx)
        resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp["Pragma"] = "no-cache"
        resp["Expires"] = "0"
        return resp

    ctx: dict = {}
    ctx.update(_api_paths_ctx())
    ctx.update(build_legal_context())
    return render(request, "ragapp/news.html", ctx)


# ─────────────────────────────────────────────
# web_qa_view — CSRF (JSON API)
# ─────────────────────────────────────────────
@csrf_protect
@require_http_methods(["POST"])
def web_qa_view(request: HttpRequest):
    try:
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except Exception:
            payload = request.POST

        q = (payload.get("q") or payload.get("query") or payload.get("question") or "").strip()
        if not q:
            return _fail("query가 비었습니다.", status_code=400)

        # ✅ PII 먼저 차단 (quota/외부호출/로그 전에)
        blocked, kind = _guard_pii_or_none(q, fail_closed=True)
        if blocked:
            msg = _pii_block_msg(kind)
            return _ok(
                {
                    "mode": "blocked",
                    "msg": "PII 차단",
                    "answer_text": msg,
                    "answer": msg,
                    "sources": [],
                    "hits": [], 
                    "log_id": None, 
                    "model": _env_model_direct(),
                    "code": "PII_BLOCKED",
                    "pii_kind": kind,
                }
            )
        
        # ✅ web safety guard (quota/외부호출/로그 전에)
        if is_web_safety_blocked(q):
            return web_blocked_json()

        dd = decide_domain(q)
        if dd and getattr(dd, "action", None) == "block":
            msg = getattr(dd, "message", "") or "요청을 처리할 수 없습니다."
            return _ok({
                "mode": "blocked",
                "code": "DOMAIN_BLOCKED",
                "domain": getattr(dd, "domain", "") or "",
                "answer_text": msg,
                "answer": msg,
                "sources": [],
                "hits": [],
                "log_id": None,
                "model": _env_model_direct(),
            })
        
        allowed, limit, used = check_and_increment_usage(request, "web")
        if not allowed:
            return _fail(
                "오늘 사용할 수 있는 웹 검색 횟수를 모두 사용했습니다.",
                extra={"code": "limit_exceeded", "kind": "web", "limit": limit, "used": used},
                status_code=429,
            )

        ans_text, headlines = _unpack_answer_sources(gemini_answer_with_news(q, ctx=_time_ctx_kst()))

        # ✅ sources 정규화 (home.web_search와 동일 스키마)
        srcs: list[dict] = []
        for h in (headlines or []):
            if isinstance(h, dict):
                raw_url = (h.get("url") or "").strip()
                clean_url = resolve_final_url(raw_url) if raw_url else ""

                srcs.append(
                    {
                        "title": (h.get("title") or clean_url or raw_url or "(제목 없음)"),
                        "url": clean_url or raw_url,
                        "snippet": (h.get("snippet") or h.get("summary") or ""),
                        "source": (h.get("source") or ""),
                        "raw_url": raw_url,  # (옵션)
                    }
                )
            else:
                srcs.append({"title": str(h), "url": "", "snippet": "", "source": ""})

        srcs = [
            s for s in (srcs or [])
            if isinstance(s, dict) and (((s.get("title") or "").strip()) or ((s.get("url") or "").strip()))
        ]

        log_obj = _create_simple_log(
            request=request,
            mode="gemini",
            question=q,
            answer_excerpt=(ans_text or ""),
            is_error=False,
            error_msg="",
            sources=srcs,
            meta={"where": "web_qa_view"},
        )

        return _ok(
            {
                "mode": "web",
                "msg": _web_msg_from_sources(srcs),
                "code": ("NO_SOURCES" if not srcs else "OK"),  # (옵션) 프론트 분기용
                "answer_text": ans_text or "",
                "answer": ans_text or "",
                "sources": srcs,
                "model": _env_model_direct(),
                "log_id": getattr(log_obj, "id", None),
            }
        )

    except Exception as e:
        log.exception("web_qa_view 실패")
        _create_simple_log(
            request=request,
            mode="gemini",
            question="(web_qa_view)",
            answer_excerpt="",
            is_error=True,
            error_msg=str(e),
            sources=[],
            meta={"where": "web_qa_view", "stage": "exception"},
        )
        return _fail(f"웹 QA 오류: {e}")


# ─────────────────────────────────────────────
# 공용: 세션 ID + 대화 로그 헬퍼 (QARAG/실시간 콘솔 공용 사용)
# ─────────────────────────────────────────────
def _append_chat_history(request: HttpRequest, q: str, a: str, *, max_items: int = 15) -> None:
    hist = request.session.get("chat_history", [])
    if not isinstance(hist, list):
        hist = []

    def _cap(s: str, n: int) -> str:
        s = (s or "").strip()
        return s if len(s) <= n else (s[: n - 1] + "…")

    hist.append({"q": _cap(q, 600), "a": _cap(a, 2000)})

    # ✅ 최근 N개만 유지 (세션 폭증 방지)
    if len(hist) > max_items:
        hist = hist[-max_items:]

    request.session["chat_history"] = hist
    request.session.modified = True

def get_chat_session_id(request: HttpRequest) -> str:
    sid = request.session.get("chat_session_id")
    if not sid:
        sid = secrets.token_hex(16)
        request.session["chat_session_id"] = sid
        request.session.modified = True
    return sid


def log_chat_message(
    *,
    request: HttpRequest,
    session_id: str,
    channel: str,
    mode: str,
    role: str,
    message_type: str,
    question: str,
    content: str,
    answer_excerpt: str = "",
    sources: list | None = None,
    meta_extra: dict | None = None,
    is_error: bool = False,
    error_msg: str = "",
) -> ChatQueryLog:
    """
    ✅ ChatQueryLog 스키마가 환경마다 다를 수 있으니,
    - 신규 스키마면 필드 존재 여부(_has_field) 보고 안전하게 저장
    - 구(레거시) 스키마면 _create_simple_log로 폴백
    """
    client_ip = client_ip_for_log(request)
    meta = dict(meta_extra or {})
    meta.setdefault("path", request.path)

    # 핵심 필드(신규 대화로그 스키마)의 존재 여부로 분기
    has_new = (
        _has_field(ChatQueryLog, "role")
        and _has_field(ChatQueryLog, "message_type")
        and _has_field(ChatQueryLog, "content")
    )

    if not has_new:
        # ✅ 레거시 스키마 폴백 (필드 불일치로 500 방지)
        return _create_simple_log(
            request=request,
            mode=mode,
            question=question,
            answer_excerpt=(answer_excerpt or "")[:500],
            is_error=is_error,
            error_msg=error_msg or "",
            sources=(sources or []),
            meta={"where": (meta_extra or {}).get("where") or "log_chat_message", **(meta_extra or {})},
        )

    kwargs: dict = {}

    # 신규 스키마: 있는 필드만 안전하게 채움
    if _has_field(ChatQueryLog, "created_at"):
        kwargs["created_at"] = timezone.now()
    if _has_field(ChatQueryLog, "session_id"):
        kwargs["session_id"] = session_id
    if _has_field(ChatQueryLog, "channel"):
        kwargs["channel"] = channel
    if _has_field(ChatQueryLog, "mode"):
        kwargs["mode"] = mode
    if _has_field(ChatQueryLog, "role"):
        kwargs["role"] = role
    if _has_field(ChatQueryLog, "message_type"):
        kwargs["message_type"] = message_type
    if _has_field(ChatQueryLog, "question"):
        kwargs["question"] = question
    if _has_field(ChatQueryLog, "content"):
        kwargs["content"] = content
    if _has_field(ChatQueryLog, "answer_excerpt"):
        kwargs["answer_excerpt"] = (answer_excerpt or "")[:500]
    if _has_field(ChatQueryLog, "client_ip"):
        kwargs["client_ip"] = client_ip
    if _has_field(ChatQueryLog, "is_error"):
        kwargs["is_error"] = bool(is_error)
    if _has_field(ChatQueryLog, "error_msg"):
        kwargs["error_msg"] = (error_msg or "")
    if _has_field(ChatQueryLog, "was_helpful"):
        kwargs["was_helpful"] = None
    if _has_field(ChatQueryLog, "feedback"):
        kwargs["feedback"] = ""
    if _has_field(ChatQueryLog, "sources"):
        kwargs["sources"] = (sources or [])
    if _has_field(ChatQueryLog, "meta"):
        kwargs["meta"] = meta

    # legal 필드(있으면만)
    if _has_field(ChatQueryLog, "legal_basis"):
        kwargs["legal_basis"] = "consent"
    if _has_field(ChatQueryLog, "consent_version"):
        kwargs["consent_version"] = ""
    if _has_field(ChatQueryLog, "consent_log"):
        kwargs["consent_log"] = None
    if _has_field(ChatQueryLog, "legal_hold"):
        kwargs["legal_hold"] = False
    if _has_field(ChatQueryLog, "delete_at"):
        kwargs["delete_at"] = None

    return ChatQueryLog.objects.create(**kwargs)


# ─────────────────────────────────────────────
# ✅ API: RAG QA (POST + CSRF)
# ─────────────────────────────────────────────
@csrf_protect
@require_http_methods(["POST"])
def rag_qa_view(request: HttpRequest):
    from django.utils.html import escape

    def _build_faq_html(q_txt: str, a_txt: str) -> str:
        q_safe = escape(q_txt or "")
        a_safe = escape(a_txt or "").replace("\n", "<br/>")
        return (
            '<div class="qarag-faq-card">'
            '  <div class="qarag-faq-card-title">🔍 자주 묻는 질문</div>'
            f'  <div class="qarag-faq-q"><strong>Q.</strong> {q_safe}</div>'
            f'  <div class="qarag-faq-card-body">{a_safe}</div>'
            "</div>"
        )

    def _serialize_log_entry(entry: ChatQueryLog) -> dict:
        def g(name, default=""):
            return getattr(entry, name, default)

        created = g("created_at", None)
        try:
            created_at = created.isoformat() if created else ""
        except Exception:
            created_at = ""

        return {
            "id": g("id", None),
            "role": g("role", ""),
            "message_type": g("message_type", ""),
            "mode": g("mode", ""),
            "channel": g("channel", ""),
            "content": g("content", g("answer_excerpt", "")),
            "created_at": created_at,
        }

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST

    q = (payload.get("query") or payload.get("q") or payload.get("question") or "").strip()
    if not q:
        return _fail("query가 비었습니다.", status_code=400)

    # ✅ PII 먼저 차단 (quota/로그/검색 전에)  → 400으로 종료
    blocked, kind = _guard_pii_or_none(q, fail_closed=True)
    if blocked:
        msg = _pii_block_msg(kind)

        # ✅ PII는 원문 로그 저장 금지(가능하면 마스킹)
        q_red = redact_pii(q) if callable(redact_pii) else q

        session_id = get_chat_session_id(request)

        # (선택) 로그도 남기고 싶으면 - 이제 log_chat_message가 안전해졌으니 OK
        try:
            user_log = log_chat_message(
                request=request,
                session_id=session_id,
                channel="qarag",
                mode="blocked",
                role="user",
                message_type="query",
                question=q_red,
                content=q_red,
                sources=[],
                meta_extra={"where": "rag_qa_view", "pii_blocked": True, "pii_kind": kind},
            )
            answer_log = log_chat_message(
                request=request,
                session_id=session_id,
                channel="qarag",
                mode="blocked",
                role="assistant",
                message_type="answer",
                question=q_red,
                content=msg,
                answer_excerpt=msg[:500],
                sources=[],
                meta_extra={"where": "rag_qa_view", "pii_blocked": True, "pii_kind": kind},
            )
            messages_payload = [_serialize_log_entry(user_log), _serialize_log_entry(answer_log)]
            log_id = getattr(answer_log, "id", None)
        except Exception:
            messages_payload = []
            log_id = None

        # ✅ assistant.js가 blocked 카드로 그리도록 ok:true + mode:"blocked"
        return _ok(
            {
                "mode": "blocked",
                "model": _env_model_rag(),
                "guard_reason": "pii", 
                "guard_hit": True,
                "answer_text": msg,
                "answer": msg,
                "answer_html": "",
                "hits": [],
                "sources": [],
                "log_id": log_id,
                "session_id": session_id,
                "code": "PII_BLOCKED",
                "pii_kind": kind,
                "messages": messages_payload,
            }
        )
    
    dd = decide_domain(q)
    if dd and getattr(dd, "action", None) == "block":
        msg = getattr(dd, "message", "") or "요청을 처리할 수 없습니다."
        return _ok(
            {
                "mode": "blocked",
                "code": "DOMAIN_BLOCKED",
                "domain": getattr(dd, "domain", "") or "",
                "model": _env_model_rag(),
                "guard_hit": True,
                "guard_reason": "domain",
                "answer_text": msg,
                "answer": msg,
                "answer_html": "",
                "hits": [],
                "sources": [],
                "log_id": None,
            }
        )

    if dd and getattr(dd, "action", None) == "clarify":
        ask = getattr(dd, "ask", "") or "질문을 조금만 더 구체적으로 알려줄래요?"
        domain = getattr(dd, "domain", "") or ""
        return _ok(
            {
                "mode": "clarify",
                "code": "NEED_CLARIFY",
                "domain": domain,
                "ask": ask,
                "answer_text": ask,
                "answer": ask,
                "sources": [],
                "hits": [],
                "log_id": None,
                "model": _env_model_rag(),
            }
        )

    # ✅ (B) 최신/실시간/현재·직전년도 감지 → 웹검색 안내 (비용 0)
    hint = decide_web_hint(q)
    if hint:
        msg = hint["message"]
        return _ok(
            {
                "mode": "hint_web",
                "code": hint.get("code") or "HINT_WEB",
                "model": _env_model_rag(),
                "answer_text": msg,
                "answer": msg,
                "answer_html": "",
                "hits": [],
                "sources": [],
                "log_id": None,
            }
        )

    # ✅ (C) 그 외에만 quota 소비
    try:
        allowed, limit, used = check_and_increment_usage(request, "rag")
    except Exception as e:
        log.exception("usage limiter(rag) 실패: %s", e)
        return _fail("사용량 체크 오류로 요청을 처리할 수 없습니다.", status_code=503)

    if not allowed:
        return _fail(
            "오늘 사용할 수 있는 RAG 질문 횟수를 모두 사용했습니다. 내일 다시 이용해 주세요.",
            extra={"code": "limit_exceeded", "kind": "rag", "limit": limit, "used": used},
            status_code=429,
        )

    session_id = get_chat_session_id(request)

    user_log = log_chat_message(
        request=request,
        session_id=session_id,
        channel="qarag",
        mode="rag",
        role="user",
        message_type="query",
        question=q,
        content=q,
        sources=[],
        meta_extra={"where": "rag_qa_view"},
    )

    if is_sensitive_question(q):
        safe_ans = safe_block_response(q)
        user_log.mode = "blocked"
        user_log.save(update_fields=["mode"])

        answer_log = log_chat_message(
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
            meta_extra={"where": "rag_qa_view", "blocked": True},
        )

        _append_chat_history(request, q, safe_ans)

        return _ok(
            {
                "mode": "blocked",
                "model": _env_model_rag(),
                "answer_text": safe_ans,
                "answer": safe_ans,
                "guard_hit": True,
                "guard_reason": "safety",
                "answer_html": "",
                "hits": [],
                "sources": [], 
                "log_id": answer_log.id,
                "session_id": session_id,
                "messages": [_serialize_log_entry(user_log), _serialize_log_entry(answer_log)],
            }
        )

    try:
        faq_answer = find_best_faq_answer(q)
    except Exception as e:
        log.warning("find_best_faq_answer 예외: %s", e)
        faq_answer = None

    if faq_answer:
        user_log.mode = "faq"
        user_log.save(update_fields=["mode"])

        answer_log = log_chat_message(
            request=request,
            session_id=session_id,
            channel="qarag",
            mode="faq",
            role="assistant",
            message_type="answer",
            question=q,
            content=faq_answer,
            answer_excerpt=(faq_answer or "")[:500],
            sources=[],
            meta_extra={"where": "rag_qa_view", "faq": True},
        )

        log_success(
            mode_label="faq",
            query_text=q,
            preview="faq hit",
            request=request,
            extra={"where": "rag_qa_view", "faq": True},
        )

        _append_chat_history(request, q, faq_answer)

        return _ok(
            {
                "mode": "faq",
                "msg": "FAQ 답변", 
                "model": _env_model_rag(),
                "answer_text": faq_answer,
                "answer": faq_answer,
                "answer_html": _build_faq_html(q, faq_answer),
                "hits": [],
                "sources": [], 
                "log_id": answer_log.id,
                "session_id": session_id,
                "messages": [_serialize_log_entry(user_log), _serialize_log_entry(answer_log)],
            }
        )

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
            rag_text = res.get("answer") or res.get("text") or ""
            used_hits = res.get("hits") or res.get("sources") or []
        else:
            rag_text = str(res)
            used_hits = []

        hits_payload = []
        for i, h in enumerate(used_hits or [], start=1):
            if isinstance(h, dict):
                m = h.get("meta") or {}
                hits_payload.append(
                    {
                        "idx": i,
                        "title": m.get("title") or m.get("url") or h.get("title") or h.get("url") or "문서",
                        "source": m.get("source_name") or m.get("source") or h.get("source") or "",
                        "url": m.get("url") or h.get("url") or "",
                        "snippet": _hit_text(h),
                        "score": m.get("score") if "score" in m else h.get("score"),
                    }
                )
            else:
                hits_payload.append({"idx": i, "title": str(h), "source": "", "url": "", "snippet": "", "score": None})

        user_log.mode = "rag"
        user_log.save(update_fields=["mode"])

        answer_log = log_chat_message(
            request=request,
            session_id=session_id,
            channel="qarag",
            mode="rag",
            role="assistant",
            message_type="answer",
            question=q,
            content=rag_text,
            answer_excerpt=(rag_text or "")[:500],
            sources=hits_payload,
            meta_extra={"where": "rag_qa_view", "hit_count": len(used_hits or [])},
        )

        log_success(
            mode_label="rag",
            query_text=q,
            preview="rag ok (rag_qa_view)",
            request=request,
            extra={"where": "rag_qa_view", "hit_count": len(used_hits or [])},
        )

        _append_chat_history(request, q, rag_text)

        ui_max_cards = int(getattr(settings, "RAG_EVIDENCE_MAX_CARDS", 5))
        ui_min_score = getattr(settings, "RAG_EVIDENCE_MIN_SCORE", None)
        try:
            ui_min_score = float(ui_min_score) if ui_min_score is not None else None
        except Exception:
            ui_min_score = None

        hits_payload_ui = filter_source_cards_dicts(
            hits_payload,
            max_cards=ui_max_cards,
            min_score=ui_min_score,
            drop_boilerplate=True,
            dedupe=True,
        )

        return _ok(
            {
                "mode": "rag",
                "msg": "RAG 검색 완료",
                "model": _env_model_rag(),
                "answer_text": rag_text,
                "answer": rag_text,
                "answer_html": "",
                "hits": hits_payload_ui,
                "sources": hits_payload_ui,
                "log_id": answer_log.id,
                "session_id": session_id,
                "messages": [_serialize_log_entry(user_log), _serialize_log_entry(answer_log)],
            }
        )

    except Exception as e:
        err_log = log_chat_message(
            request=request,
            session_id=session_id,
            channel="qarag",
            mode="rag",
            role="system",
            message_type="error",
            question=q,
            content="",
            sources=[],
            meta_extra={"where": "rag_qa_view", "stage": "rag_answer_grounded"},
            is_error=True,
            error_msg=str(e),
        )
        log_error(
            mode_label="rag",
            query_text=q,
            err_msg=str(e),
            request=request,
            extra={"where": "rag_qa_view", "stage": "rag_answer_grounded", "log_id": err_log.id},
        )
        return _fail(f"RAG 검색 실패: {e}")


# ─────────────────────────────────────────────
# ✅ API: RAG 대화 (POST + CSRF)
# ─────────────────────────────────────────────
@csrf_protect
@require_http_methods(["POST"])
def qa_rag_chat(request: HttpRequest):
    # 1) payload 먼저 파싱
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception as e:
        _create_simple_log(
            request=request,
            mode="rag",
            question="(invalid json body)",
            answer_excerpt="",
            is_error=True,
            error_msg=f"invalid json: {e}",
            sources=[],
            meta={"where": "qa_rag_chat"},
        )
        return JsonResponse(
            {"ok": False, "error": f"invalid json: {e}"},
            status=400,
            json_dumps_params={"ensure_ascii": False},
        )

    q = (payload.get("question") or payload.get("q") or "").strip()
    if not q:
        _create_simple_log(
            request=request,
            mode="rag",
            question="(empty question)",
            answer_excerpt="",
            is_error=True,
            error_msg="empty question",
            sources=[],
            meta={"where": "qa_rag_chat"},
        )
        return _fail("질문이 비어 있습니다.", status_code=400)

    # 2) ✅ PII 먼저 차단 (quota 소비 전에)
    blocked, kind = _guard_pii_or_none(q, fail_closed=True)
    if blocked:
        msg = _pii_block_msg(kind)
        return _ok(
            {
                "mode": "blocked",
                "msg": "PII 차단",
                "answer_text": msg,
                "answer": msg,
                "hits": [],
                "sources": [],
                "model": _env_model_rag(),
                "code": "PII_BLOCKED",
                "pii_kind": kind,
            }
        )
    
    # ✅ (A) 도메인 라우팅: block / clarify (비용 0)
    dd = decide_domain(q)
    if dd and getattr(dd, "action", None) == "block":
        msg = getattr(dd, "message", "") or "요청을 처리할 수 없습니다."
        return _ok(
            {
                "mode": "blocked",
                "code": "DOMAIN_BLOCKED",
                "domain": getattr(dd, "domain", "") or "",
                "msg": "도메인 차단",
                "answer_text": msg,
                "answer": msg,
                "hits": [],
                "sources": [],
                "model": _env_model_rag(),
                "guard_hit": True,
                "guard_reason": "domain",
            }
        )

    if dd and getattr(dd, "action", None) == "clarify":
        ask = getattr(dd, "ask", "") or "질문을 조금만 더 구체적으로 알려줄래요?"
        domain = getattr(dd, "domain", "") or ""
        return _ok(
            {
                "mode": "clarify",
                "code": "NEED_CLARIFY",
                "domain": domain,
                "ask": ask,
                "answer_text": ask,
                "answer": ask,
                "hits": [],
                "sources": [],
                "model": _env_model_rag(),
            }
        )

    # ✅ (B) 최신/실시간/현재·직전년도 감지 → 웹검색 안내 (비용 0)
    hint = decide_web_hint(q)
    if hint:
        msg = hint["message"]
        return _ok(
            {
                "mode": "hint_web",
                "code": hint.get("code") or "HINT_WEB",
                "msg": "웹검색 안내",
                "answer_text": msg,
                "answer": msg,
                "hits": [],
                "sources": [],
                "model": _env_model_rag(),
            }
        )

    # 3) ✅ 그 다음 quota
    try:
        allowed, limit, used = check_and_increment_usage(request, "rag")
    except Exception as e:
        log.exception("usage limiter(rag) 실패: %s", e)
        return _fail("사용량 체크 오류로 요청을 처리할 수 없습니다.", status_code=503)

    if not allowed:
        return _fail(
            "오늘 사용할 수 있는 RAG 질문 횟수를 모두 사용했습니다. 내일 다시 이용해 주세요.",
            extra={"code": "limit_exceeded", "kind": "rag", "limit": limit, "used": used},
            status_code=429,
        )

    topk = max(1, int(getattr(settings, "RAG_QUERY_TOPK", 5)))
    fallback_topk = max(topk + 5, int(getattr(settings, "RAG_FALLBACK_TOPK", 12)))
    max_sources = int(getattr(settings, "RAG_MAX_SOURCES", 8))

    try:
        history_list = request.session.get("chat_history", [])

        res = rag_answer_grounded_with_history(
            q,
            history_list,
            base_retriever_func=rag_answer_grounded,
            initial_topk=topk,
            fallback_topk=fallback_topk,
            max_sources=max_sources,
        )

        if isinstance(res, tuple) and len(res) >= 2:
            rag_answer_text, used_hits = res[0], res[1]
        elif isinstance(res, dict):
            rag_answer_text = res.get("answer") or res.get("text") or ""
            used_hits = res.get("hits") or res.get("sources") or []
        else:
            rag_answer_text = str(res)
            used_hits = []

        hits_payload = []
        for i, h in enumerate(used_hits or [], start=1):
            if isinstance(h, dict):
                m = h.get("meta") or {}
                hits_payload.append(
                    {
                        "idx": i,
                        "title": m.get("title") or m.get("url") or h.get("title") or h.get("url") or "문서",
                        "source": m.get("source_name") or m.get("source") or h.get("source") or "",
                        "url": m.get("url") or h.get("url") or "",
                        "snippet": (_hit_text(h))[:500],
                        "score": h.get("score"),
                    }
                )
            else:
                hits_payload.append({"idx": i, "title": str(h), "source": "", "url": "", "snippet": "", "score": None})

        _create_simple_log(
            request=request,
            mode="rag",
            question=q,
            answer_excerpt=(rag_answer_text or ""),
            is_error=False,
            error_msg="",
            sources=hits_payload,
            meta={"where": "qa_rag_chat", "hit_count": len(hits_payload)},
        )

        log_success(
            mode_label="rag",
            query_text=q,
            preview="qa_rag_chat ok",
            request=request,
            extra={"where": "qa_rag_chat", "hit_count": len(hits_payload)},
        )

        _append_chat_history(request, q, rag_answer_text, max_items=15)

        ui_max_cards = int(getattr(settings, "RAG_EVIDENCE_MAX_CARDS", 5))
        ui_min_score = getattr(settings, "RAG_EVIDENCE_MIN_SCORE", None)
        try:
            ui_min_score = float(ui_min_score) if ui_min_score is not None else None
        except Exception:
            ui_min_score = None

        hits_payload_ui = filter_source_cards_dicts(
            hits_payload,
            max_cards=ui_max_cards,
            min_score=ui_min_score,
            drop_boilerplate=True,
            dedupe=True,
        )

        normalized_sources = _normalize_rag_sources(hits_payload_ui)

        return _ok(
            {
                "mode": "rag",
                "msg": "RAG 검색 완료",
                "answer_text": rag_answer_text or "(빈 응답)",
                "answer": rag_answer_text or "(빈 응답)",
                "hits": hits_payload_ui,
                "sources": hits_payload_ui,
                "sources_norm": normalized_sources,
                "model": _env_model_rag(),
            }
        )

    except Exception as e:
        _create_simple_log(
            request=request,
            mode="rag",
            question=q,
            answer_excerpt="",
            is_error=True,
            error_msg=str(e),
            sources=[],
            meta={"where": "qa_rag_chat", "stage": "rag_answer_grounded"},
        )
        log_error(
            mode_label="rag",
            query_text=q,
            err_msg=str(e),
            request=request,
            extra={"where": "qa_rag_chat", "stage": "rag_answer_grounded"},
        )
        return _fail(f"RAG 오류: {e}")


@staff_member_required
@ensure_csrf_cookie
def assistant_view(request: HttpRequest) -> HttpResponse:
    ctx = {"model_name_rag": _env_model_rag()}
    return render(request, "ragapp/assistant.html", ctx)


# ─────────────────────────────────────────────
# indexto_chroma_safe (로컬 shim)
# - 실제 저장은 vdb_store.vdb_upsert(듀얼: SQLite + (옵션)Chroma)를 사용
# ─────────────────────────────────────────────
def indexto_chroma_safe(query: str, answer: str, news_list: list[dict]):
    from ragapp.services.news_services import _chunk_text, _sha, _slug, _iso

    # vdb_upsert는 vdb_store 우선, 없으면 (구버전)vector_store 폴백
    try:
        from ragapp.services.vdb_store import vdb_upsert  # type: ignore
    except Exception:
        try:
            from ragapp.services.vector_store import vdb_upsert  # type: ignore
        except Exception:
            raise RuntimeError("벡터 DB 어댑터(vdb_upsert)를 찾을 수 없습니다.")

    # ✅ DB 중복 스킵 유틸 (URL 우선, 없으면 title+source)
    # - sqlite_hybrid_store에서 가져오던 것을 vdb_store로 변경
    try:
        from ragapp.services.vdb_store import (
            vdb_url_exists as url_exists,
            vdb_title_source_exists as title_source_exists,
        )  # type: ignore
    except Exception:
        url_exists = None  # type: ignore
        title_source_exists = None  # type: ignore

    def _norm_url_local(u: str) -> str:
        u = (u or "").strip()
        if not u:
            return ""
        return u.split("#", 1)[0].strip()

    size = int(getattr(settings, "EMBED_CHUNK_SIZE", 1600))
    overlap = int(getattr(settings, "EMBED_CHUNK_OVERLAP", 200))
    min_body = int(getattr(settings, "MIN_NEWS_BODY_CHARS", 400))
    now = datetime.utcnow().isoformat()

    all_ids: list[str] = []
    all_docs: list[str] = []
    all_metas: list[dict] = []

    # ─────────────────────────
    # 1) 답변 텍스트 인덱싱
    # ─────────────────────────
    a_chunks = _chunk_text(answer or "", size=size, overlap=overlap)
    base_a = f"answer:{_sha(query)}"
    for i, ch in enumerate(a_chunks):
        ch_clean = (ch or "").strip()
        if not ch_clean:
            continue
        all_ids.append(f"{base_a}:{i}")
        all_docs.append(ch_clean)
        all_metas.append(
            {
                "source": "web_answer",
                "title": "웹검색 답변",
                "question": query,
                "ingested_at": now,
            }
        )

    # ─────────────────────────
    # 2) 뉴스 메타/본문 인덱싱 (중복 스킵 통합)
    # ─────────────────────────
    news_summaries: list[dict] = []
    skipped_count = 0

    # 같은 요청 batch 내에서도 중복 스킵
    seen_url: set[str] = set()
    seen_ts: set[tuple[str, str]] = set()  # (title, source)

    for art in (news_list or []):
        url = (art.get("final_url") or art.get("url") or "").strip()
        title = (art.get("title") or "").strip() or "(제목 없음)"
        source_name = (art.get("source") or "").strip()
        published_at = str(art.get("published_at") or "").strip()
        body = (art.get("news_body") or "").strip()

        url_norm = _norm_url_local(url)
        url_key = url_norm.lower() if url_norm else ""
        ts_key = (title.strip().lower(), source_name.strip().lower())

        # ── (A) batch 내 중복 스킵 ─────────────────────────
        if url_key:
            if url_key in seen_url:
                skipped_count += 1
                news_summaries.append(
                    {
                        "title": title,
                        "url": url_norm or url,
                        "chunks": 0,
                        "meta_only": True,
                        "skipped": True,
                        "skip_reason": "within_batch_url",
                    }
                )
                continue
            seen_url.add(url_key)
        else:
            # URL 없으면 title+source로 스킵 (source 비면 오탐 가능 → source 없으면 batch 스킵 안 함 권장)
            if source_name and title and title != "(제목 없음)":
                if ts_key in seen_ts:
                    skipped_count += 1
                    news_summaries.append(
                        {
                            "title": title,
                            "url": url_norm or url,
                            "chunks": 0,
                            "meta_only": True,
                            "skipped": True,
                            "skip_reason": "within_batch_title_source",
                        }
                    )
                    continue
                seen_ts.add(ts_key)

        # ── (B) DB 중복 스킵 ───────────────────────────────
        try:
            if url_norm and callable(url_exists):
                if url_exists(url_norm):
                    skipped_count += 1
                    news_summaries.append(
                        {
                            "title": title,
                            "url": url_norm,
                            "chunks": 0,
                            "meta_only": True,
                            "skipped": True,
                            "skip_reason": "db_url",
                        }
                    )
                    continue
            elif (not url_norm) and callable(title_source_exists):
                # URL이 없으면 title+source로만 중복 판정
                # source가 없으면 오탐 위험 → 이 경우는 DB 스킵 판정 안 함(권장)
                if source_name and title and title != "(제목 없음)":
                    if title_source_exists(title, source_name):
                        skipped_count += 1
                        news_summaries.append(
                            {
                                "title": title,
                                "url": url_norm or url,
                                "chunks": 0,
                                "meta_only": True,
                                "skipped": True,
                                "skip_reason": "db_title_source",
                            }
                        )
                        continue
        except Exception:
            # 중복 체크가 터져도 인덱싱 자체는 진행(서비스 안정성)
            pass

        # ✅ ID 충돌 줄이기: URL 있으면 URL, 없으면 source|title|published_at
        base_key = url_norm or (f"{source_name}|{title}|{published_at}".strip())
        base = f"news:{_slug(title)}:{_sha(base_key)}"

        meta_doc_lines = [
            f"[META ONLY] {title}",
            f"URL: {url_norm}" if url_norm else "URL: (없음)",
            f"출처: {source_name}",
            f"게시: {_iso(published_at)}",
            (art.get("snippet") or "")[:500],
        ]
        meta_doc = "\n".join([ln for ln in meta_doc_lines if ln]).strip()

        meta_only = (len(body) < min_body) or _WEB_INGEST_META_ONLY

        all_ids.append(f"{base}:meta")
        all_docs.append(meta_doc)
        all_metas.append(
            {
                "source": "news",
                "meta_only": meta_only,
                "url": url_norm,
                "title": title,
                "source_name": source_name,
                "published_at": published_at,
                "ingested_at": now,
            }
        )

        chunks_for_this_news = 1

        if (not _WEB_INGEST_META_ONLY) and len(body) >= min_body:
            body_chunks = _chunk_text(body, size=size, overlap=overlap)
            body_cnt = 0
            for j, ch in enumerate(body_chunks):
                ch_clean = (ch or "").strip()
                if not ch_clean:
                    continue
                all_ids.append(f"{base}:{j}")
                all_docs.append(ch_clean)
                all_metas.append(
                    {
                        "source": "news",
                        "url": url_norm,
                        "title": title,
                        "source_name": source_name,
                        "published_at": published_at,
                        "ingested_at": now,
                    }
                )
                body_cnt += 1
            chunks_for_this_news += body_cnt

        news_summaries.append(
            {
                "title": title,
                "url": url_norm,
                "chunks": chunks_for_this_news,
                "meta_only": meta_only,
                "skipped": False,
                "skip_reason": "",
            }
        )

    # ─────────────────────────
    # 3) 업서트 준비/임베딩/저장
    # ─────────────────────────
    clean_rows = [
        (doc_id, doc_text, meta)
        for (doc_id, doc_text, meta) in zip(all_ids, all_docs, all_metas)
        if isinstance(doc_text, str) and doc_text.strip()
    ]

    if not clean_rows:
        return {
            "inserted": 0,
            "answer_chunks": 0,
            "news_total_chunks": 0,
            "news_items": news_summaries,
            "skipped_news": skipped_count,
            "collection": getattr(settings, "VECTOR_DB_LABEL", getattr(settings, "CHROMA_COLLECTION", "")),
            "dir": _vector_db_path(),
            "ingested_at": now,
            "note": "인덱싱할 데이터가 없습니다.",
        }

    final_ids, final_docs, final_metas = map(list, zip(*clean_rows))

    try:
        from ragapp.services.vertex_embed import embed_texts as _embed_texts  # type: ignore
    except Exception:
        from ragapp.services.news_services import _embed_texts  # type: ignore

    embs = _embed_texts(final_docs)

    vdb_result = vdb_upsert(final_ids, final_docs, final_metas, embs)

    ans_chunks = sum(1 for m in final_metas if m.get("source") == "web_answer")
    news_chunks = sum(1 for m in final_metas if m.get("source") == "news" and not m.get("meta_only"))

    dj = vdb_result.get("django_ragchunk") if isinstance(vdb_result, dict) else None

    return {
        "inserted": len(final_ids),
        "answer_chunks": ans_chunks,
        "news_total_chunks": news_chunks,
        "news_items": news_summaries,
        "skipped_news": skipped_count,
        "collection": getattr(settings, "VECTOR_DB_LABEL", getattr(settings, "CHROMA_COLLECTION", "")),
        "dir": _vector_db_path(),
        "ingested_at": now,
        "vdb": vdb_result,
        "django_ragchunk": dj,
    }


# ─────────────────────────────────────────────
# ✅ QARAG → 실시간 상담 콘솔 연결 요청
# (사용자 요구: 웹검색/ RAG검색만 PII 차단. 라이브 채팅 요청은 PII 차단 안 함)
# ─────────────────────────────────────────────
@csrf_protect
@require_http_methods(["POST"])
def qarag_live_chat_request(request: HttpRequest):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST

    q = (payload.get("question") or payload.get("q") or "").strip()
    client_label = (payload.get("client_label") or "").strip() or "웹 QARAG 사용자"

    if not q:
        return _fail("question이 비었습니다.", status_code=400)

    # ✅ 라이브채팅 요청은 '차단'하지 말고,
    # ✅ 저장/노출용 텍스트만 마스킹해서 직원에게 PII가 직접 노출되지 않게 한다.
    pii_hit, pii_kind = _guard_pii_or_none(q, fail_closed=False)
    q_store = redact_pii(q) if pii_hit else q

    room_id = f"client-{timezone.now().strftime('%Y%m%d%H%M%S')}-{os.urandom(3).hex()[:6]}"

    # ✅ 최신 모델이 LiveChatSession이면 그걸 우선 사용
    try:
        from ragapp.models import LiveChatSession  # type: ignore

        fields = {f.name for f in LiveChatSession._meta.get_fields()}
        kwargs: dict = {}

        # room/code 둘 중 존재하는 필드에 매핑
        if "room" in fields:
            kwargs["room"] = room_id
        elif "code" in fields:
            kwargs["code"] = room_id

        if "status" in fields:
            kwargs["status"] = "waiting"

        if "source" in fields:
            kwargs["source"] = "qarag"

        # 이름/라벨 저장 (있는 필드만)
        for k in ("user_name", "client_name", "client_label", "name"):
            if k in fields:
                kwargs[k] = client_label
                break

        # 마지막 질문 저장 (있는 필드만)
        if "last_question" in fields:
            kwargs["last_question"] = q_store
        elif "session_note" in fields:
            kwargs["session_note"] = q_store
        elif "memo" in fields:
            kwargs["memo"] = q_store

        # 상세 JSON 필드가 있으면 더 풍부하게
        if "session_detail" in fields:
            kwargs["session_detail"] = {
                "last_question": q_store,
                "pii_redacted": bool(pii_hit),
                "pii_kind": pii_kind,
            }

        session = LiveChatSession.objects.create(**kwargs)

        return _ok(
            {
                "room_id": room_id,
                "status": getattr(session, "status", "waiting"),
                "pii_redacted": bool(pii_hit),
                "pii_kind": pii_kind,
            }
        )

    except Exception:
        # ✅ 레거시 LiveChatRoom 폴백 (프로젝트에 남아있다면)
        from ragapp.models import LiveChatRoom  # 모델 존재 전제

        room = LiveChatRoom.objects.create(
            room_id=room_id,
            client_label=client_label,
            last_question=q_store,
            status="waiting",
        )

        return _ok(
            {
                "room_id": room.room_id,
                "status": room.status,
                "pii_redacted": bool(pii_hit),
                "pii_kind": pii_kind,
            }
        )
