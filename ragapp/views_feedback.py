# ragapp/views_feedback.py
from __future__ import annotations

import json
import hashlib
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from django.http import JsonResponse, HttpRequest
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from ragapp.models import QaragFeedback, ChatQueryLog

log = logging.getLogger(__name__)

# (선택) PII 가드가 있으면 마스킹 적용 (없어도 동작)
try:
    from ragapp.utils.pii_guard import detect_pii, redact_pii  # type: ignore
except Exception:
    detect_pii = None
    redact_pii = None


# ─────────────────────────────────────────────────────────────
# Limits (DB/로그 폭발 방지)
# ─────────────────────────────────────────────────────────────
MAX_QUESTION_LEN = 2000
MAX_ANSWER_LEN = 12000
MAX_COMMENT_LEN = 800

MAX_SOURCES = 30
MAX_SOURCE_TITLE = 200
MAX_SOURCE_URL = 1000

MAX_REASON_LEN = 60
MAX_REASONS = 10

MAX_SESSION_ID_LEN = 80


def _hash_ip(ip: str) -> str:
    if not ip:
        return ""
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()[:64]


def _read_json(request: HttpRequest) -> Dict[str, Any]:
    try:
        raw = (request.body or b"").decode("utf-8") or "{}"
        return json.loads(raw)
    except Exception:
        return {}


def _norm_text(v: Any, max_len: int) -> str:
    s = ("" if v is None else str(v)).strip()
    if max_len and len(s) > max_len:
        return s[:max_len]
    return s


def _parse_bool(v: Any) -> Optional[bool]:
    if v is None:
        return None
    if v in (True, False):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "t"):
        return True
    if s in ("0", "false", "no", "n", "f"):
        return False
    return None


def _parse_client_ts(v: Any) -> Optional[timezone.datetime]:
    s = _norm_text(v, 64)
    if not s:
        return None
    dt = parse_datetime(s)
    if not dt:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _norm_sources(src: Any) -> List[Dict[str, str]]:
    """
    sources:
      - [{title,url}, ...]  또는  ["https://...", ...]
      - 최대 MAX_SOURCES개, title/url 길이 제한
    """
    if not isinstance(src, list):
        return []
    out: List[Dict[str, str]] = []
    for item in src[:MAX_SOURCES]:
        if item is None:
            continue

        if isinstance(item, dict):
            title = _norm_text(item.get("title") or item.get("name") or "", MAX_SOURCE_TITLE)
            url = _norm_text(item.get("url") or item.get("href") or "", MAX_SOURCE_URL)
            if not title and not url:
                continue
            out.append({"title": title, "url": url})
            continue

        if isinstance(item, str):
            url = _norm_text(item, MAX_SOURCE_URL)
            if url:
                out.append({"title": "", "url": url})

    return out


def _norm_reasons(v: Any) -> List[str]:
    """
    reasons:
      - "incorrect" (str) 또는 ["incorrect","grounding"...] (list)
    """
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        return [s[:MAX_REASON_LEN]] if s else []
    if isinstance(v, list):
        out: List[str] = []
        for x in v:
            s = str(x).strip()
            if not s:
                continue
            out.append(s[:MAX_REASON_LEN])
            if len(out) >= MAX_REASONS:
                break
        return out
    return []


def _maybe_pii_redact(question: str, comment: str) -> tuple[str, str, bool]:
    """
    PII 가드가 있으면 question/comment를 마스킹.
    """
    if not detect_pii or not redact_pii:
        return question, comment, False
    try:
        has = bool(detect_pii(question) or detect_pii(comment))
        if not has:
            return question, comment, False
        return redact_pii(question), redact_pii(comment), True
    except Exception:
        return question, comment, False


def _find_chat_log(log_id: Any) -> Optional[ChatQueryLog]:
    """
    log_id가 오면 ChatQueryLog와 연결(있으면).
    """
    if not log_id:
        return None
    try:
        pk = int(str(log_id).strip())
    except Exception:
        return None

    try:
        return ChatQueryLog.objects.get(pk=pk)
    except ChatQueryLog.DoesNotExist:
        return None
    except Exception:
        return None


def _same_origin_ok(request: HttpRequest) -> bool:
    """
    CSRF exempt API라도, Origin/Referer 기반으로 same-origin만 허용.
    - 브라우저 fetch는 보통 Origin을 보냄
    - Origin이 없으면 Referer로 보조 체크
    - 둘 다 없으면(서버/크론/테스트) 허용
    """
    try:
        host = (request.get_host() or "").lower()
        if not host:
            return True

        origin = (request.META.get("HTTP_ORIGIN") or "").strip()
        if origin:
            o = urlparse(origin)
            return (o.netloc or "").lower() == host

        referer = (request.META.get("HTTP_REFERER") or "").strip()
        if referer:
            r = urlparse(referer)
            return (r.netloc or "").lower() == host

        return True
    except Exception:
        return True


def _json_error(code: str, status: int = 400) -> JsonResponse:
    return JsonResponse(
        {"ok": False, "error": code},
        status=status,
        json_dumps_params={"ensure_ascii": False},
    )


def _append_extra(comment: str, extra: Dict[str, Any]) -> str:
    """
    모델 변경 없이 comment에 extra JSON을 꼬리로 붙임.
    """
    if not extra:
        return comment
    try:
        tail = json.dumps(extra, ensure_ascii=False)
        if comment:
            return comment + "\n\n" + tail
        return tail
    except Exception:
        return comment


# ─────────────────────────────────────────────────────────────
# QARAG 전용
# ─────────────────────────────────────────────────────────────
@require_POST
@csrf_exempt
def api_qarag_feedback(request: HttpRequest) -> JsonResponse:
    """
    질문 챗봇(QARAG) 전용 피드백 저장 API

    기대 JSON:
    {
      "helpful": true/false,
      "comment": "문장...",
      "question": "사용자 질문",
      "answer": "챗봇 답변",
      "log_id": 123,            # 선택: ChatQueryLog PK
      "session_id": "sessionid" # 선택
    }
    """
    try:
        if not _same_origin_ok(request):
            return _json_error("forbidden_origin", status=403)

        payload = _read_json(request)
        if not payload:
            return _json_error("invalid_json", status=400)

        helpful_b = _parse_bool(payload.get("helpful", None))

        question = _norm_text(payload.get("question") or "", MAX_QUESTION_LEN)
        answer = _norm_text(payload.get("answer") or "", MAX_ANSWER_LEN)
        comment = _norm_text(payload.get("comment") or "", MAX_COMMENT_LEN)

        session_id = _norm_text(payload.get("session_id") or "", MAX_SESSION_ID_LEN)
        log_id = payload.get("log_id") or payload.get("chat_log_id")
        chat_log = _find_chat_log(log_id)

        ip = request.META.get("REMOTE_ADDR", "") or ""
        ua = request.META.get("HTTP_USER_AGENT", "") or ""
        ip_h = _hash_ip(ip)

        question, comment, pii_redacted = _maybe_pii_redact(question, comment)

        extra = {
            "from_ui": "qarag",
            "ip_h": ip_h,
            "ua": (ua[:300] if ua else ""),
            "pii_redacted": bool(pii_redacted),
        }

        fb = QaragFeedback.objects.create(
            chat_log=chat_log,
            session_id=session_id,
            question=question,
            answer=answer,
            is_helpful=helpful_b,
            comment=_append_extra(comment, extra),
        )

        return JsonResponse(
            {"ok": True, "id": fb.id},
            json_dumps_params={"ensure_ascii": False},
        )

    except Exception as e:
        log.exception("QARAG_FEEDBACK_ERROR")
        return JsonResponse(
            {"ok": False, "error": str(e) or e.__class__.__name__},
            status=500,
            json_dumps_params={"ensure_ascii": False},
        )


# ─────────────────────────────────────────────────────────────
# 공용(assistant/news/widget 공용) - ✅ urls.py에서 쓰는 엔드포인트
# ─────────────────────────────────────────────────────────────
def _submit_feedback_impl(request: HttpRequest) -> JsonResponse:
    """
    내부 공용 구현(데코레이터만 다르게 붙여서 재사용)
    """
    payload = _read_json(request)
    if not payload:
        return _json_error("invalid_json", status=400)

    # helpful: widget("helpful") / 기존("is_helpful") 모두 지원
    helpful = payload.get("helpful", None)
    if helpful is None:
        helpful = payload.get("is_helpful", None)
    helpful_b = _parse_bool(helpful)

    question = _norm_text(payload.get("question") or "", MAX_QUESTION_LEN)
    answer = _norm_text(payload.get("answer") or "", MAX_ANSWER_LEN)
    comment = _norm_text(payload.get("comment") or "", MAX_COMMENT_LEN)

    reasons = _norm_reasons(payload.get("reasons") or payload.get("reason"))
    sources = _norm_sources(payload.get("sources"))

    from_ui = _norm_text(payload.get("from_ui") or "assistant", 50)
    stage = _norm_text(payload.get("stage") or "thumb", 20)
    answer_type = _norm_text(payload.get("answer_type") or "web", 20).lower()
    mode = _norm_text(payload.get("mode") or "", 20)
    page = _norm_text(payload.get("page") or request.path, 200)
    client_ts = _parse_client_ts(payload.get("client_ts"))

    session_id = _norm_text(payload.get("session_id") or "", MAX_SESSION_ID_LEN)
    log_id = payload.get("log_id") or payload.get("chat_log_id")
    chat_log = _find_chat_log(log_id)

    ip = request.META.get("REMOTE_ADDR", "") or ""
    ua = request.META.get("HTTP_USER_AGENT", "") or ""
    ip_h = _hash_ip(ip)

    question, comment, pii_redacted = _maybe_pii_redact(question, comment)

    extra = {
        "from_ui": from_ui,
        "stage": stage,
        "answer_type": answer_type,
        "mode": mode,
        "reasons": reasons,
        "sources": sources,
        "page": page,
        "client_ts": (client_ts.isoformat() if client_ts else ""),
        "ip_h": ip_h,
        "ua": (ua[:300] if ua else ""),
        "pii_redacted": bool(pii_redacted),
    }

    fb = QaragFeedback.objects.create(
        chat_log=chat_log,
        session_id=session_id,
        question=question,
        answer=answer,
        is_helpful=helpful_b,
        comment=_append_extra(comment, extra),
    )

    return JsonResponse(
        {"ok": True, "id": fb.id},
        json_dumps_params={"ensure_ascii": False},
    )


@require_POST
@csrf_exempt
def api_submit_feedback(request: HttpRequest) -> JsonResponse:
    """
    ✅ API 엔드포인트용 (urls.py에서 /api/feedback, /api/submit_feedback 모두 여기로 연결)
    - CSRF exempt 대신 Origin/Referer same-origin 체크로 충돌 최소화
    """
    try:
        if not _same_origin_ok(request):
            return _json_error("forbidden_origin", status=403)
        return _submit_feedback_impl(request)
    except Exception as e:
        log.exception("FEEDBACK_API_ERROR")
        return JsonResponse(
            {"ok": False, "error": str(e) or e.__class__.__name__},
            status=500,
            json_dumps_params={"ensure_ascii": False},
        )


@require_POST
@csrf_protect
def submit_feedback(request: HttpRequest) -> JsonResponse:
    """
    (선택 유지) CSRF 보호가 필요한 폼/페이지에서 직접 POST할 때 사용 가능.
    """
    try:
        return _submit_feedback_impl(request)
    except Exception as e:
        log.exception("FEEDBACK_ERROR")
        return JsonResponse(
            {"ok": False, "error": str(e) or e.__class__.__name__},
            status=500,
            json_dumps_params={"ensure_ascii": False},
        )
