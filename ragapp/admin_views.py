# ragapp/admin_views.py
from __future__ import annotations

from typing import Callable, Optional, List, Dict, Any
from importlib import import_module
from datetime import timedelta
import os
import re
import json
import logging
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.template import TemplateDoesNotExist
from django.urls import reverse
from django.shortcuts import render, redirect
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.http import require_http_methods, require_GET
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.utils import timezone
from django.db.models import Q
from django.template.loader import render_to_string

from ragapp.models import (
    FeedbackLog,
    FeedbackReview,   # (현재 파일에서 직접 사용은 없지만, 모델 관계 확인용으로 유지 가능)
    QaragFeedback,
    Feedback,
    LiveChatSession,
    ChatQueryLog,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# 동적 import 유틸
# ─────────────────────────────────────────────────────────────────────
def _import_attr(dotted: str) -> Optional[Callable]:
    try:
        if ":" in dotted:
            mod_path, attr = dotted.split(":", 1)
        else:
            mod_path, attr = dotted.rsplit(".", 1)
        mod = import_module(mod_path)
        return getattr(mod, attr)
    except Exception:
        return None


def _first_impl(candidates: List[str]) -> Optional[Callable]:
    for d in candidates:
        fn = _import_attr(d)
        if callable(fn):
            return fn
    return None


def _rev_any(*names: str, default: str = "/") -> str:
    """
    reverse가 깨질 수 있는 환경(네임스페이스/URL 분기) 대비용.
    앞에서부터 시도하고, 다 실패하면 default 반환.
    """
    for n in names:
        try:
            return reverse(n)
        except Exception:
            continue
    return default


# URL 정규화: HTML 붙여넣음/이상 값 → 링크 비활성화
def _normalize_url(v: object) -> str | None:
    if not v:
        return None
    s = str(v).strip()
    if "<" in s or ">" in s:  # HTML/스크립트 혼입 차단
        return None
    if s.startswith(("http://", "https://", "/", "mailto:", "tel:")):
        return s
    if re.match(r"^(www\.)?[a-z0-9.-]+\.[a-z]{2,}(/.*)?$", s, re.I):
        return "https://" + s
    return None


def _tobool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip().lower()
    return s not in ("0", "false", "no", "off", "", "none", "null")


# ─────────────────────────────────────────────────────────────────────
# 상담 로그 조회 헬퍼
# ─────────────────────────────────────────────────────────────────────
def fetch_chat_messages(session_id: str) -> list[ChatQueryLog]:
    return list(
        ChatQueryLog.objects.filter(session_id=session_id).order_by("created_at", "id")
    )


# ─────────────────────────────────────────────────────────────────────
# 실시간 콘솔 (레거시 진입점)
# ─────────────────────────────────────────────────────────────────────
@staff_member_required
def live_console_view(request: HttpRequest) -> HttpResponse:
    """
    (레거시) 실시간 상담 콘솔 화면
    - ?session_id=... 없으면 최근 세션 하나 골라서 띄움
    - 내부적으로는 live_chat_view 와 같은 템플릿을 사용
    """
    session_id = (request.GET.get("session_id") or "").strip()

    if not session_id:
        last_log = (
            ChatQueryLog.objects.exclude(session_id="")
            .order_by("-created_at")
            .first()
        )
        session_id = last_log.session_id if last_log else ""

    room = session_id or "master"

    # ✅ ragadmin 네임스페이스 우선
    url = _rev_any("ragadmin:live_chat", "live_chat", default="/ragadmin/live-chat/")
    return redirect(f"{url}?room={room}")


# ─────────────────────────────────────────────────────────────────────
# 운영자 콘솔에서 답변 전송 API
# ─────────────────────────────────────────────────────────────────────
@require_http_methods(["POST"])
@staff_member_required
@csrf_protect
def live_chat_send_view(request: HttpRequest) -> JsonResponse:
    """
    운영자가 콘솔에서 답변을 보낼 때 호출되는 API
    - LiveChatSession 이 종료 상태이면 추가 전송 차단
    - ChatQueryLog 에 assistant/answer 형태로 한 줄 남김
    """
    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)

    room = (data.get("room") or data.get("session_id") or "").strip()
    text = (data.get("text") or "").strip()

    if not room or not text:
        return JsonResponse({"ok": False, "error": "missing_params"}, status=400)

    # 🔒 종료 세션이면 발송 막기
    sess_obj = None
    try:
        field_names = {
            f.name for f in LiveChatSession._meta.get_fields() if hasattr(f, "attname")
        }
        qs = LiveChatSession.objects.all()

        if "room" in field_names:
            sess_obj = qs.filter(room=room).order_by("-id").first()

        if sess_obj is None and room.isdigit():
            sess_obj = qs.filter(pk=int(room)).first()
    except Exception:
        sess_obj = None

    if sess_obj is not None:
        try:
            field_names = {
                f.name for f in LiveChatSession._meta.get_fields() if hasattr(f, "attname")
            }

            ended = False
            status = getattr(sess_obj, "status", None)
            if isinstance(status, str):
                s_norm = status.strip().lower()
                if s_norm in ("done", "종료", "ended", "closed", "완료", "saved", "deleted", "ended_need_save"):
                    ended = True

            if "is_active" in field_names:
                if getattr(sess_obj, "is_active", True) is False:
                    ended = True

            if "ended_at" in field_names:
                if getattr(sess_obj, "ended_at", None):
                    ended = True

            if ended:
                return JsonResponse({"ok": False, "error": "ended_session"}, status=400)
        except Exception:
            log.exception("live_chat_send_view: session ended check error")

    # ✅ 순환참조 방지: 여기서 늦은 import
    try:
        from ragapp.news_views.news_views import log_chat_message  # noqa
    except Exception as e:
        log.exception("log_chat_message import failed: %s", e)
        return JsonResponse({"ok": False, "error": "log_helper_missing"}, status=500)

    msg = log_chat_message(
        request=request,
        session_id=room,
        channel="live_console",
        mode="rag",
        role="assistant",
        message_type="answer",
        question=f"(operator_reply to {room})",
        content=text,
        answer_excerpt=text[:300],
        sources=[],
        meta_extra={"from": "admin_console"},
    )

    return JsonResponse(
        {
            "ok": True,
            "message": {
                "id": msg.id,
                "role": msg.role,
                "message_type": msg.message_type,
                "content": msg.content,
                "created_at": msg.created_at.isoformat(),
            },
        }
    )


# ─────────────────────────────────────────────────────────────────────
# 공통 컨텍스트
# ─────────────────────────────────────────────────────────────────────
def _common_ctx(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base_dir = getattr(settings, "BASE_DIR", Path("."))

    auto_ingest = _tobool(
        getattr(
            settings,
            "AUTO_INGEST_AFTER_GEMINI",
            os.environ.get("AUTO_INGEST_AFTER_GEMINI", "1"),
        )
    )

    ctx: Dict[str, Any] = {
        "MEDIA_URL": getattr(settings, "MEDIA_URL", "-"),
        "MEDIA_ROOT": getattr(settings, "MEDIA_ROOT", "-"),
        "VECTOR_DB_PATH": os.environ.get("VECTOR_DB_PATH")
        or str(Path(base_dir) / "vector_store.sqlite3"),
        "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),
        "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
        "AUTO_INGEST_AFTER_GEMINI": auto_ingest,
    }
    if extra:
        ctx.update(extra)
    return ctx


# ─────────────────────────────────────────────────────────────────────
# 템플릿 키 누락 방지용 안전 기본값
# ─────────────────────────────────────────────────────────────────────
CRAWL_SAFE_DEFAULTS: Dict[str, Any] = {
    "q": "",
    "rss_q": "",
    "urls": [],
    "rss_list": [],
    "gemini_answer": "",
    "answer_text": "",
    "answer_md": "",
    "answer_html": "",
    "final_answer": "",
    "answer_sources": [],
    "sources": [],
    "ingest_results": [],
    "ingest_count": 0,
    "ingest_errors": [],
    "error": None,
    "diagnostics": {},
}

FAQ_SUGGEST_SAFE_DEFAULTS: Dict[str, Any] = {"candidates": [], "suggestions": [], "limit": 50, "error": None}
FAQ_PROMOTE_SAFE_DEFAULTS: Dict[str, Any] = {"promoted": [], "error": None}
LIVE_CHAT_SAFE_DEFAULTS: Dict[str, Any] = {"history": [], "error": None}
LEGAL_SAFE_DEFAULTS: Dict[str, Any] = {"legal_config": None, "error": None}


def _fill_answer_aliases(ctx: Dict[str, Any]) -> None:
    rep = ctx.get("answer_text") or ctx.get("gemini_answer") or ctx.get("final_answer") or ""
    for key in ("answer_text", "final_answer", "gemini_answer", "answer_md", "answer_html"):
        ctx.setdefault(key, "")
    if not ctx.get("answer_text"):
        ctx["answer_text"] = rep
    if not ctx.get("final_answer"):
        ctx["final_answer"] = rep
    if not ctx.get("answer_md"):
        ctx["answer_md"] = rep
    if not ctx.get("answer_html"):
        ctx["answer_html"] = rep


# ─────────────────────────────────────────────────────────────────────
# 1) 뉴스 크롤링
# ─────────────────────────────────────────────────────────────────────
_IMPL_CRAWL = _first_impl(
    [
        "ragapp.news_views.views_crawl:crawl_news_view",
        "ragapp.news_views.views_crawl.crawl_news_view",
        "ragapp.admin_views.crawl:crawl_news_view",
        "ragapp.admin_views.crawl.crawl_news_view",
        "ragapp.news_views.news_views:crawl_news",
        "ragapp.news_views.news_views.crawl_news",
    ]
)


@staff_member_required
@csrf_protect
def crawl_news_view(request: HttpRequest) -> HttpResponse:
    if _IMPL_CRAWL:
        return _IMPL_CRAWL(request)
    ctx = _common_ctx({"title": "뉴스 크롤링 & 인덱싱", **CRAWL_SAFE_DEFAULTS})
    _fill_answer_aliases(ctx)
    return render(request, "ragadmin/crawl_news.html", ctx)


# ─────────────────────────────────────────────────────────────────────
# 2) FAQ 추천
# ─────────────────────────────────────────────────────────────────────
_IMPL_FAQ_SUGGEST = _first_impl(
    [
        "ragapp.admin_views.faq:faq_suggest_view",
        "ragapp.admin_views.faq.faq_suggest_view",
        "ragapp.news_views.news_views:faq_suggest",
        "ragapp.news_views.news_views.faq_suggest",
    ]
)


@staff_member_required
@csrf_protect
def faq_suggest_view(request: HttpRequest) -> HttpResponse:
    if _IMPL_FAQ_SUGGEST:
        return _IMPL_FAQ_SUGGEST(request)
    ctx = _common_ctx({"title": "FAQ 추천", **FAQ_SUGGEST_SAFE_DEFAULTS})
    if ctx.get("candidates") and not ctx.get("suggestions"):
        ctx["suggestions"] = ctx["candidates"]
    return render(request, "ragadmin/faq_suggest.html", ctx)


# ─────────────────────────────────────────────────────────────────────
# 3) FAQ 승격
# ─────────────────────────────────────────────────────────────────────
_IMPL_FAQ_PROMOTE = _first_impl(
    [
        "ragapp.admin_views.faq:faq_promote_view",
        "ragapp.admin_views.faq.faq_promote_view",
    ]
)


@staff_member_required
@csrf_protect
def faq_promote_view(request: HttpRequest) -> HttpResponse:
    if _IMPL_FAQ_PROMOTE:
        return _IMPL_FAQ_PROMOTE(request)
    ctx = _common_ctx({"title": "FAQ 승격", **FAQ_PROMOTE_SAFE_DEFAULTS})
    try:
        return render(request, "ragadmin/faq_promote.html", ctx)
    except TemplateDoesNotExist:
        return HttpResponse("<h1>FAQ 승격</h1><p>템플릿이 아직 없습니다.</p>")


# ─────────────────────────────────────────────────────────────────────
# 4) 라이브 챗 (운영자 콘솔 + 최근 세션 리스트)
# ─────────────────────────────────────────────────────────────────────
_IMPL_LIVE_CHAT = _first_impl(
    [
        "ragapp.admin_views.live:live_chat_view",
        "ragapp.admin_views.live.live_chat_view",
    ]
)


@staff_member_required
@csrf_protect
def live_chat_view(request: HttpRequest) -> HttpResponse:
    if _IMPL_LIVE_CHAT:
        return _IMPL_LIVE_CHAT(request)

    room = (
        request.GET.get("room")
        or request.GET.get("session_id")
        or request.POST.get("room")
        or request.session.get("live_room")
        or "master"
    )
    room = (room or "").strip() or "master"
    request.session["live_room"] = room

    messages_qs = ChatQueryLog.objects.filter(session_id=room).order_by("created_at", "id")

    # LiveChatSession 목록
    try:
        field_names = {f.name for f in LiveChatSession._meta.get_fields() if hasattr(f, "attname")}
        qs = LiveChatSession.objects.all()
        if "created_at" in field_names:
            qs = qs.order_by("-created_at")
        elif "requested_at" in field_names:
            qs = qs.order_by("-requested_at")
        else:
            qs = qs.order_by("-id")
        raw_sessions = list(qs[:30])
    except Exception:
        raw_sessions = []

    def _first_attr(obj, *names, default=None):
        for n in names:
            if hasattr(obj, n):
                v = getattr(obj, n, None)
                if v not in (None, ""):
                    return v
        return default

    sessions: list[dict[str, Any]] = []
    for obj in raw_sessions:
        created = _first_attr(obj, "created_at", "requested_at")
        code = _first_attr(obj, "code", "ticket_code", "queue_code", "short_id") or str(getattr(obj, "pk", ""))
        note = _first_attr(obj, "session_note", "memo", "note", default="") or ""
        sess_type = _first_attr(obj, "session_type", "type", default="") or ""

        sessions.append(
            {
                "id": getattr(obj, "id", None),
                "code": code,
                "status": _first_attr(obj, "status", default="") or "",
                "room": _first_attr(obj, "room", default="") or "",
                "created_at": created,
                "session_type": sess_type,
                "session_note": note,
                "note": note,  # 템플릿 호환
                "memo": note,  # 템플릿 호환
            }
        )

    current_session: dict[str, Any] | None = None
    for s in sessions:
        try:
            if room and (s.get("room") == room or str(s.get("id") or "") == room or s.get("code") == room):
                current_session = s
                break
        except Exception:
            continue

    ctx = _common_ctx({"title": "라이브 챗", **LIVE_CHAT_SAFE_DEFAULTS})
    ctx.update(
        {
            "room": room,
            "session_id": room,
            "initial_room": room,
            "messages": messages_qs,
            "sessions": sessions,
            "current_session": current_session,
            "csp_nonce": getattr(request, "csp_nonce", None),
        }
    )
    return render(request, "ragadmin/live_chat.html", ctx)


# ─────────────────────────────────────────────────────────────────────
# 5) 오늘 세션 일괄 종료 / 개별 삭제(옵션)
# ─────────────────────────────────────────────────────────────────────
@require_http_methods(["POST"])
@staff_member_required
@csrf_protect
def live_chat_cleanup_view(request: HttpRequest) -> JsonResponse:
    try:
        try:
            payload = json.loads(request.body or "{}")
        except json.JSONDecodeError:
            payload = request.POST

        session_id = payload.get("session_id")
        mode = (payload.get("mode") or "today").strip()

        field_names = {f.name for f in LiveChatSession._meta.get_fields() if hasattr(f, "attname")}
        qs = LiveChatSession.objects.all()

        # 개별 삭제
        if session_id:
            try:
                if isinstance(session_id, str) and session_id.isdigit():
                    session_id = int(session_id)
            except Exception:
                pass
            deleted_count, _ = qs.filter(pk=session_id).delete()
            return JsonResponse({"ok": True, "deleted": deleted_count})

        # 오늘 세션 일괄 종료
        today = timezone.localdate()
        if "created_at" in field_names:
            qs = qs.filter(created_at__date=today)
        elif "requested_at" in field_names:
            qs = qs.filter(requested_at__date=today)

        if "status" in field_names:
            qs = qs.exclude(status__in=["ended", "종료", "saved", "deleted", "ended_need_save"])

        update_kwargs: dict = {}
        now = timezone.now()

        if "status" in field_names:
            update_kwargs["status"] = "ended"
        if "is_active" in field_names:
            update_kwargs["is_active"] = False
        if "ended_at" in field_names:
            update_kwargs["ended_at"] = now

        updated = qs.update(**update_kwargs) if update_kwargs else qs.count()
        return JsonResponse({"ok": True, "updated": updated})

    except Exception as e:
        log.exception("live_chat_cleanup_view error")
        return JsonResponse({"ok": False, "error": str(e)}, status=500)


# ─────────────────────────────────────────────────────────────────────
# 6) 법적 설정
# ─────────────────────────────────────────────────────────────────────
_IMPL_LEGAL_ENTRY = _first_impl(
    [
        "ragapp.admin_views.legal:legal_config_entrypoint",
        "ragapp.admin_views.legal.legal_config_entrypoint",
    ]
)


@staff_member_required
@csrf_protect
def legal_config_entrypoint(request: HttpRequest) -> HttpResponse:
    if _IMPL_LEGAL_ENTRY:
        return _IMPL_LEGAL_ENTRY(request)

    ctx = _common_ctx({"title": "법적 설정", **LEGAL_SAFE_DEFAULTS})
    snap = _legal_config_snapshot()
    if snap is not None:
        ctx["legal_config"] = snap
    return render(request, "ragadmin/legal_config.html", ctx)

# ─────────────────────────────────────────────────────────────────────
# 7) 이미지 pending 검수 (승인/거절)
# ─────────────────────────────────────────────────────────────────────
_IMPL_MEDIA_PENDING_ADMIN = _first_impl(
    [
        "ragapp.feature_views:media_pending_admin_view",
        "ragapp.feature_views.media_pending_admin_view",
        "ragapp.media_views.pending:media_pending_admin_view",
        "ragapp.media_views.pending.media_pending_admin_view",
    ]
)

_IMPL_MEDIA_PENDING_LIST = _first_impl(
    [
        "ragapp.feature_views:api_media_pending_list",
        "ragapp.feature_views.api_media_pending_list",
        "ragapp.media_views.pending:api_media_pending_list",
        "ragapp.media_views.pending.api_media_pending_list",
    ]
)

_IMPL_MEDIA_PENDING_APPROVE = _first_impl(
    [
        "ragapp.feature_views:api_media_pending_approve",
        "ragapp.feature_views.api_media_pending_approve",
        "ragapp.media_views.pending:api_media_pending_approve",
        "ragapp.media_views.pending.api_media_pending_approve",
    ]
)

_IMPL_MEDIA_PENDING_REJECT = _first_impl(
    [
        "ragapp.feature_views:api_media_pending_reject",
        "ragapp.feature_views.api_media_pending_reject",
        "ragapp.media_views.pending:api_media_pending_reject",
        "ragapp.media_views.pending.api_media_pending_reject",
    ]
)


@staff_member_required
@ensure_csrf_cookie
def media_pending_admin_view(request: HttpRequest) -> HttpResponse:
    """
    스태프 전용: pending 업로드 검수 화면
    """
    if _IMPL_MEDIA_PENDING_ADMIN:
        return _IMPL_MEDIA_PENDING_ADMIN(request)

    # ✅ fallback: 템플릿만이라도 렌더 (구현을 아직 다른 파일에 안 넣었을 때)
    return render(request, "ragadmin/media_pending_admin.html", {"title": "이미지 승인 대기함"})


@staff_member_required
@require_GET
def api_media_pending_list(request: HttpRequest) -> JsonResponse:
    if _IMPL_MEDIA_PENDING_LIST:
        return _IMPL_MEDIA_PENDING_LIST(request)
    return JsonResponse({"ok": False, "error": "media_pending_list_impl_missing"}, status=500)


@require_http_methods(["POST"])
@staff_member_required
@csrf_protect
def api_media_pending_approve(request: HttpRequest) -> JsonResponse:
    if _IMPL_MEDIA_PENDING_APPROVE:
        return _IMPL_MEDIA_PENDING_APPROVE(request)
    return JsonResponse({"ok": False, "error": "media_pending_approve_impl_missing"}, status=500)


@require_http_methods(["POST"])
@staff_member_required
@csrf_protect
def api_media_pending_reject(request: HttpRequest) -> JsonResponse:
    if _IMPL_MEDIA_PENDING_REJECT:
        return _IMPL_MEDIA_PENDING_REJECT(request)
    return JsonResponse({"ok": False, "error": "media_pending_reject_impl_missing"}, status=500)


# ─────────────────────────────────────────────────────────────────────
# 5-1) 상담 기록 저장 API
# ─────────────────────────────────────────────────────────────────────
@require_http_methods(["POST"])
@staff_member_required
@csrf_protect
def live_chat_save_session_view(request: HttpRequest) -> JsonResponse:
    try:
        try:
            payload = json.loads(request.body or "{}")
        except json.JSONDecodeError:
            payload = request.POST

        room = (payload.get("room") or "").strip()
        session_type = (payload.get("session_type") or "").strip()
        session_note = (payload.get("session_note") or "").strip()
        session_detail = (payload.get("session_detail") or "").strip()

        if not room:
            return JsonResponse({"ok": False, "error": "missing_room"}, status=400)

        if not (session_type or session_note or session_detail):
            return JsonResponse({"ok": False, "error": "empty_session_meta"}, status=400)

        field_names = {f.name for f in LiveChatSession._meta.get_fields() if hasattr(f, "attname")}

        # ✅ union( | ) 쓰지 말고, 2단계로 안전 조회
        qs = LiveChatSession.objects.all()
        sess = None

        if "room" in field_names:
            qs_room = qs.filter(room=room)
            if "created_at" in field_names:
                qs_room = qs_room.order_by("-created_at")
            elif "requested_at" in field_names:
                qs_room = qs_room.order_by("-requested_at")
            else:
                qs_room = qs_room.order_by("-id")
            sess = qs_room.first()

        if sess is None and room.isdigit():
            sess = LiveChatSession.objects.filter(pk=int(room)).first()

        if not sess:
            return JsonResponse({"ok": False, "error": "session_not_found"}, status=404)

        now = timezone.now()

        short = session_note.strip()
        detail = session_detail.strip()
        if short and detail:
            combined = f"{short}\n\n{detail}"
        else:
            combined = short or detail

        update_kwargs: Dict[str, Any] = {}

        if "session_type" in field_names and session_type:
            update_kwargs["session_type"] = session_type

        if combined:
            if "session_note" in field_names:
                update_kwargs["session_note"] = combined
            if "memo" in field_names:
                update_kwargs["memo"] = combined
            if "note" in field_names:
                update_kwargs["note"] = combined

        if "status" in field_names:
            update_kwargs["status"] = getattr(sess, "status", None) or "ended"
        if "is_active" in field_names:
            update_kwargs["is_active"] = False
        if "ended_at" in field_names:
            update_kwargs["ended_at"] = getattr(sess, "ended_at", None) or now
        if "last_message_at" in field_names:
            update_kwargs["last_message_at"] = getattr(sess, "last_message_at", None) or now

        if update_kwargs:
            LiveChatSession.objects.filter(pk=sess.pk).update(**update_kwargs)

        return JsonResponse({"ok": True, "session_id": sess.pk, "room": getattr(sess, "room", room)})
    except Exception as e:
        log.exception("live_chat_save_session_view error")
        return JsonResponse({"ok": False, "error": str(e)}, status=500)


# ─────────────────────────────────────────────────────────────────────
# LegalConfig 스냅샷 로더
# ─────────────────────────────────────────────────────────────────────
def _legal_config_snapshot():
    try:
        from ragapp.models import LegalConfig
    except Exception:
        return None

    qs = LegalConfig.objects.all()
    for flag in ("is_active", "active", "enabled"):
        if hasattr(LegalConfig, flag):
            qs = qs.filter(**{flag: True})
            break

    for ts in ("updated_at", "modified", "created_at", "created", "id"):
        if hasattr(LegalConfig, ts):
            qs = qs.order_by(f"-{ts}")
            break

    inst = qs.first()
    if not inst:
        return None

    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    valmap: Dict[str, Any] = {}
    for f in inst._meta.get_fields():
        name = getattr(f, "name", None)
        if not name or not hasattr(inst, name):
            continue
        try:
            val = getattr(inst, name)
        except Exception:
            continue
        valmap[_norm(name)] = val

    def _pick(candidates=None, contains_all=None, contains_any=None, default=None):
        if candidates:
            for c in candidates:
                k = _norm(c)
                if k in valmap and valmap[k] not in (None, ""):
                    return valmap[k]
        keys = list(valmap.keys())
        if contains_all:
            toks = [_norm(t) for t in contains_all]
            for k in keys:
                if all(t in k for t in toks):
                    v = valmap[k]
                    if v not in (None, ""):
                        return v
        if contains_any:
            toks = [_norm(t) for t in contains_any]
            for k in keys:
                if any(t in k for t in toks):
                    v = valmap[k]
                    if v not in (None, ""):
                        return v
        return default

    snap: Dict[str, Any] = {
        "service_name": _pick(
            candidates=["service_name", "serviceTitle", "service", "site_name", "sitename", "app_name"],
            contains_any=["service", "sitename", "appname"],
        ),
        "operator_name": _pick(
            candidates=["operator_name", "operator", "owner_name", "owner", "provider_name", "company_name", "corp_name"],
            contains_any=["operator", "owner", "provider", "company", "corp"],
        ),
        "contact_email": _pick(
            candidates=["contact_email", "email", "contact", "support_email", "admin_email"],
            contains_any=["email"],
        ),
        "privacy_url": _pick(
            candidates=["privacy_url", "privacy_link", "policy_url"],
            contains_all=["privacy", "url"],
        )
        or os.environ.get("PRIVACY_URL")
        or getattr(settings, "PRIVACY_URL", None),
        "tos_url": _pick(
            candidates=["tos_url", "terms_url", "terms_link", "tos"],
            contains_any=["termsurl", "tosurl", "termslink", "tos"],
        )
        or os.environ.get("TERMS_URL")
        or getattr(settings, "TERMS_URL", None),
        "overseas_transfer_url": _pick(
            candidates=["overseas_transfer_url", "transfer_url", "crossborder_url", "outbound_url"],
            contains_any=["overseas", "transfer", "crossborder", "outbound"],
        )
        or os.environ.get("OVERSEAS_TRANSFER_URL")
        or getattr(settings, "OVERSEAS_TRANSFER_URL", None),
        "enable_consent_gate": _tobool(
            _pick(
                candidates=["enable_consent_gate", "consent_gate", "show_gate", "gate_required", "consent_required"],
                contains_any=["consentgate", "consent", "gate", "agree"],
            )
        ),
        "show_footer_links": _tobool(
            _pick(
                candidates=["show_footer_links", "footer_links", "footer_show_links", "show_footer", "footer_visible"],
                contains_any=["footer", "link", "visible", "show"],
            )
        ),
        "memo": _pick(candidates=["memo", "notes", "note", "description"], default=""),
    }

    snap["privacy_url"] = _normalize_url(snap.get("privacy_url") or os.environ.get("PRIVACY_URL") or getattr(settings, "PRIVACY_URL", None))
    snap["tos_url"] = _normalize_url(snap.get("tos_url") or os.environ.get("TERMS_URL") or getattr(settings, "TERMS_URL", None))
    snap["overseas_transfer_url"] = _normalize_url(
        snap.get("overseas_transfer_url") or os.environ.get("OVERSEAS_TRANSFER_URL") or getattr(settings, "OVERSEAS_TRANSFER_URL", None)
    )

    if not snap.get("privacy_url"):
        snap["privacy_url"] = _normalize_url(_rev_any("legal_privacy", default="/legal/privacy/"))
    if not snap.get("tos_url"):
        snap["tos_url"] = _normalize_url(_rev_any("legal_tos", default="/legal/tos/"))
    if not snap.get("overseas_transfer_url"):
        snap["overseas_transfer_url"] = _normalize_url(_rev_any("legal_overseas", default="/legal/overseas/"))

    def _env_true(name: str) -> bool | None:
        val = os.environ.get(name) or getattr(settings, name, None)
        if val is None:
            return None
        return _tobool(val)

    if _env_true("SHOW_FOOTER_LINKS") is True:
        snap["show_footer_links"] = True
    if _env_true("ENABLE_CONSENT_GATE") is True:
        snap["enable_consent_gate"] = True

    return snap


@staff_member_required
@require_GET
def live_chat_recent_sessions_view(request: HttpRequest) -> JsonResponse:
    """
    '최근 상담 세션' 리스트만 HTML 조각으로 반환.
    """
    try:
        field_names = {f.name for f in LiveChatSession._meta.get_fields() if hasattr(f, "attname")}
        qs = LiveChatSession.objects.all()

        if "created_at" in field_names:
            qs = qs.order_by("-created_at")
        elif "requested_at" in field_names:
            qs = qs.order_by("-requested_at")
        else:
            qs = qs.order_by("-id")

        sessions = list(qs[:30])

        html = render_to_string(
            "ragadmin/_live_chat_session_items.html",
            {"sessions": sessions},
            request=request,
        )
        return JsonResponse({"ok": True, "html": html}, json_dumps_params={"ensure_ascii": False})
    except Exception as e:
        log.exception("live_chat_recent_sessions_view error")
        return JsonResponse({"ok": False, "error": str(e)}, status=500)


@staff_member_required
def feedback_dashboard_view(request: HttpRequest) -> HttpResponse:
    """
    질문 챗봇(QARAG) + 웹/Gemini + RAG 피드백을 한 눈에 보는 간단 대시보드.
    """
    today = timezone.localdate()
    start_7d = today - timedelta(days=7)  # ✅ timezone.timedelta → datetime.timedelta

    qs_qarag = QaragFeedback.objects.all()
    qs_qarag_7d = qs_qarag.filter(created_at__date__gte=start_7d)

    qs_fb = Feedback.objects.all()
    qs_web = qs_fb.filter(answer_type="gemini")
    qs_rag = qs_fb.filter(answer_type="rag")

    ctx = {
        "today": today,
        "total_qarag": qs_qarag.count(),
        "total_qarag_7d": qs_qarag_7d.count(),
        "total_qarag_good_7d": qs_qarag_7d.filter(is_helpful=True).count(),
        "total_qarag_bad_7d": qs_qarag_7d.filter(is_helpful=False).count(),
        "total_web": qs_web.count(),
        "total_rag": qs_rag.count(),
        "total_web_good": qs_web.filter(is_helpful=True).count(),
        "total_rag_good": qs_rag.filter(is_helpful=True).count(),
    }
    return render(request, "ragadmin/feedback_dashboard.html", ctx)


@staff_member_required
def feedback_board_view(request: HttpRequest) -> HttpResponse:
    """
    통합 피드백 보드
    - helpful=False 인 것만 모아서 본다
    """
    channel = request.GET.get("channel", "all")  # all / web / rag / qa
    q = (request.GET.get("q") or "").strip()

    qs = (
        FeedbackLog.objects.filter(helpful=False)
        .select_related("review")
        .order_by("-created_at")
    )

    if channel in ("web", "rag", "qa"):
        qs = qs.filter(answer_type=channel)

    if q:
        qs = qs.filter(Q(question__icontains=q) | Q(answer__icontains=q) | Q(comment__icontains=q))

    total_count = qs.count()
    today = timezone.localdate()
    today_count = qs.filter(created_at__date=today).count()

    paginator = Paginator(qs, 30)
    page_number = request.GET.get("page") or 1
    page_obj = paginator.get_page(page_number)

    ctx = {
        "page_obj": page_obj,
        "channel": channel,
        "q": q,
        "total_count": total_count,
        "today_count": today_count,
    }
    return render(request, "ragadmin/feedback_board.html", ctx)


@staff_member_required
def runtime_dashboard(request: HttpRequest) -> HttpResponse:
    """
    RAG Admin · 실시간 런타임 모니터링
    - ?format=json 또는 X-Requested-With=XMLHttpRequest: JSON
    """
    now = timezone.now()
    today = timezone.localdate()

    window_seconds = 300
    online_count = int(cache.get("runtime:online_count", 0))
    recent_requests = int(cache.get("runtime:recent_requests", 0))
    today_total = int(cache.get("runtime:today_total", 0))
    today_429 = int(cache.get("runtime:today_429", 0))
    livechat_open = int(cache.get("runtime:livechat_open", 0))

    warn_threshold = getattr(settings, "RUNTIME_ONLINE_WARN", 2)
    busy_threshold = getattr(settings, "RUNTIME_ONLINE_BUSY", 5)

    status = "idle"
    if today_429 > 0 or online_count >= busy_threshold:
        status = "busy"
    elif online_count >= warn_threshold:
        status = "normal"

    online_snapshot = {
        "window_seconds": window_seconds,
        "online_count": online_count,
        "recent_requests": recent_requests,
        "today_total": today_total,
        "today_429": today_429,
        "livechat_open": livechat_open,
        "status": status,
    }

    media_root = getattr(settings, "MEDIA_ROOT", None)
    media_enabled = bool(media_root)
    media_exists = False
    media_file_count = 0
    media_total_mb = 0.0
    auto_purge_enabled = _tobool(os.environ.get("MEDIA_AUTO_PURGE", ""))

    if media_root:
        p = Path(media_root)
        if p.exists():
            media_exists = True
            try:
                for f in p.rglob("*"):
                    if f.is_file():
                        media_file_count += 1
                        media_total_mb += f.stat().st_size / (1024 * 1024)
            except Exception:
                pass

    media_snapshot = {
        "root": str(media_root) if media_root else "",
        "enabled": media_enabled,
        "exists": media_exists,
        "file_count": media_file_count,
        "total_mb": round(media_total_mb, 1),
        "auto_purge_enabled": auto_purge_enabled,
    }

    settings_snapshot = {
        "RETENTION_DAYS": int(getattr(settings, "RETENTION_DAYS", 0)),
        "RETENTION_DAYS_CHATLOG": int(getattr(settings, "RETENTION_DAYS_CHATLOG", 90)),
        "RETENTION_DAYS_LIVECHAT": int(getattr(settings, "RETENTION_DAYS_LIVECHAT", 180)),
    }

    snapshot = {
        "date": today.isoformat(),
        "now_display": timezone.localtime(now).strftime("%Y-%m-%d %H:%M:%S"),
        "online": online_snapshot,
        "media": media_snapshot,
        "settings": settings_snapshot,
    }

    wants_json = (
        request.GET.get("format") == "json"
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    )
    if wants_json:
        return JsonResponse(snapshot, json_dumps_params={"ensure_ascii": False})

    return render(request, "ragadmin/runtime_dashboard.html", {"snapshot": snapshot})


__all__ = [
    "crawl_news_view",
    "faq_suggest_view",
    "faq_promote_view",
    "live_chat_view",
    "live_console_view",
    "live_chat_send_view",
    "legal_config_entrypoint",
    "live_chat_cleanup_view",
    "live_chat_save_session_view",
    "live_chat_recent_sessions_view",
    "feedback_dashboard_view",
    "feedback_board_view",
    "runtime_dashboard",
    "media_pending_admin_view",
    "api_media_pending_list",
    "api_media_pending_approve",
    "api_media_pending_reject",
]
