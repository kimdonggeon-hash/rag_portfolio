# ragapp/livechat/views.py
from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta
from typing import Any, Dict, Optional, Tuple, Set, List

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, csrf_protect, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST
from ragapp.livechat.store import fetch_session_messages_for_log

from ragapp.models import LiveChatSession

# ✅ “무조건 보이게/저장되게” 핵심 유틸
from ragapp.livechat.store import (
    ensure_room_row,
    save_ws_message,
)

log = logging.getLogger(__name__)

# 채널 레이어(웹소켓 브로드캐스트) 옵션
try:  # pragma: no cover
    from channels.layers import get_channel_layer
    from asgiref.sync import async_to_sync
except Exception:  # pragma: no cover
    get_channel_layer = None
    async_to_sync = None

END_MESSAGE = (
    "상담을 종료했습니다. 추가로 궁금한 점이 생기면 언제든지 질문 챗봇이나 실시간 상담을 이용해 주세요."
)

_END_TYPES = {"end", "closed", "close"}

_CLOSED_STATUS_FALLBACKS = {
    "ended",
    "closed",
    "close",
    "done",
    "saved",
    "종료",
    "ended_need_save",
}


def _status_to_str(v: Any) -> str:
    try:
        return str(getattr(v, "value", v) or "").strip().lower()
    except Exception:
        return ""


def _closed_statuses() -> Set[str]:
    values = set(_CLOSED_STATUS_FALLBACKS)

    try:
        Status = getattr(LiveChatSession, "Status", None)
        if Status is not None:
            for name in ("ENDED", "CLOSED", "DONE", "SAVED", "ENDED_NEED_SAVE"):
                if hasattr(Status, name):
                    values.add(_status_to_str(getattr(Status, name)))
    except Exception:
        pass

    return values


def _is_livechat_closed(session: Optional[LiveChatSession]) -> bool:
    if session is None:
        return True

    status = _status_to_str(getattr(session, "status", ""))

    if status in _closed_statuses():
        return True

    if getattr(session, "ended_at", None):
        return True

    return False


# ─────────────────────────────────────
# 공통 유틸
# ─────────────────────────────────────
def _json_body(request: HttpRequest) -> Dict[str, Any]:
    try:
        if not request.body:
            return {}
        return json.loads(request.body.decode("utf-8"))
    except Exception:
        return {}


def _model_fields(model) -> Set[str]:
    """
    name + attname 둘 다 수집.
    FK면 session / session_id 둘 다 잡히게.
    """
    out: Set[str] = set()
    try:
        for f in model._meta.get_fields():
            n = getattr(f, "name", None)
            a = getattr(f, "attname", None)
            if n:
                out.add(str(n))
            if a:
                out.add(str(a))
    except Exception:
        pass
    return out


def _to_int(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        s = str(v).strip()
        if not s:
            return None
        return int(s)
    except Exception:
        return None


def _ts_ms(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        if isinstance(v, int):
            return v * 1000 if v < 10_000_000_000 else v
        if isinstance(v, float):
            i = int(v)
            return i * 1000 if i < 10_000_000_000 else i
        s = str(v).strip()
        if not s:
            return None
        if s.isdigit():
            i = int(s)
            return i * 1000 if i < 10_000_000_000 else i
    except Exception:
        pass
    return None


def _send_master(payload: Dict[str, Any]) -> None:
    """
    상담사 마스터 콘솔용 그룹(livechat_master)에 이벤트 브로드캐스트
    """
    if not get_channel_layer or not async_to_sync:
        return
    try:
        layer = get_channel_layer()
        async_to_sync(layer.group_send)(
            "livechat_master",
            {"type": "broadcast", "payload": payload},
        )
    except Exception as e:
        log.warning("send master failed: %s", e)


def _send_room(room: str, payload: Dict[str, Any]) -> None:
    """
    특정 room(방)에 이벤트 브로드캐스트
    - consumers.py의 group_name 규칙과 동일하게 맞춰서 보냄
    """
    if not get_channel_layer or not async_to_sync:
        return
    try:
        safe = []
        for ch in (room or ""):
            if ch.isalnum() or ch in ("_", "-", "."):
                safe.append(ch)
            else:
                safe.append("_")
        g = ("".join(safe)[:80] or "unknown")
        group = "livechat_room_" + g

        layer = get_channel_layer()
        async_to_sync(layer.group_send)(
            group,
            {"type": "room_message", "payload": payload},
        )
    except Exception as e:
        log.warning("send room failed: %s", e)


def _ensure_livechat_room_row(room_id: str) -> None:
    """
    ✅ room_cnt=0 방지: 세션 생성 시 LiveChatRoom row를 가능하면 보장.
    - store.ensure_room_row()는 DB 실제 컬럼 기준으로 안전하게 처리.
    """
    try:
        ensure_room_row(room=room_id)
    except Exception:
        # 절대 플로우 깨지면 안 됨
        log.warning("ensure_room_row failed (ignored)", exc_info=True)


def _has_unlogged_session(user) -> bool:
    """
    아직 '상담 기록'을 남기지 않은 종료 세션이 있는지 체크.
    """
    fields = _model_fields(LiveChatSession)

    note_fields = [f for f in ("session_note", "session_detail") if f in fields]
    if not note_fields:
        return False

    qs = LiveChatSession.objects.all()

    if "assigned_staff" in fields and user and not getattr(user, "is_anonymous", False):
        qs = qs.filter(assigned_staff=user)

    if "status" in fields:
        done_values = ["done"]
        try:
            done_values.append(LiveChatSession.Status.SAVED)  # type: ignore[attr-defined]
        except Exception:
            pass
        qs = qs.exclude(status__in=done_values)

    if "ended_at" in fields:
        qs = qs.filter(ended_at__isnull=False)

    q = Q()
    for idx, fname in enumerate(note_fields):
        cond = Q(**{f"{fname}__isnull": True}) | Q(**{f"{fname}__exact": ""})
        q = cond if idx == 0 else (q & cond)
    qs = qs.filter(q)

    return qs.exists()

def _status_value(name: str, fallback: str) -> str:
    """
    LiveChatSession.Status가 있으면 Enum 값을 사용하고,
    없으면 fallback 문자열을 사용.
    """
    try:
        Status = getattr(LiveChatSession, "Status", None)
        if Status is not None and hasattr(Status, name):
            v = getattr(Status, name)
            return str(getattr(v, "value", v))
    except Exception:
        pass
    return fallback


def _waiting_statuses() -> List[str]:
    """
    상담 요청 대기 상태로 인정할 값들.
    """
    return list(
        {
            "waiting",
            "pending",
            "대기",
            _status_value("WAITING", "waiting"),
        }
    )


def _visible_console_statuses() -> List[str]:
    """
    상담자 콘솔 왼쪽 목록에 보여줄 상태만 허용.
    waiting: 상담 요청 들어옴
    active: 상담 진행 중
    """
    return list(
        {
            "waiting",
            "pending",
            "대기",
            "active",
            "진행",
            "ended_need_save",
            "need_note",
            "saved",
            "done",
            _status_value("WAITING", "waiting"),
            _status_value("ACTIVE", "active"),
            _status_value("ENDED_NEED_SAVE", "ended_need_save"),
            _status_value("SAVED", "saved"),
        }
    )


def _active_statuses() -> List[str]:
    """
    상담 진행 중 상태로 인정할 값들.
    """
    return list(
        {
            "active",
            "진행",
            _status_value("ACTIVE", "active"),
        }
    )


def _int_setting(name: str, default: int) -> int:
    """
    settings 값이 문자열이어도 안전하게 int로 변환.
    """
    try:
        return int(getattr(settings, name, default))
    except Exception:
        return default


def _open_sessions_queryset(statuses: List[str]):
    """
    종료되지 않은 특정 상태의 상담 세션 queryset.
    """
    fields = _model_fields(LiveChatSession)

    qs = LiveChatSession.objects.all()

    if "status" in fields:
        qs = qs.filter(status__in=statuses)
    else:
        return LiveChatSession.objects.none()

    if "ended_at" in fields:
        qs = qs.filter(ended_at__isnull=True)

    return qs


def _open_session_count(statuses: List[str]) -> int:
    try:
        return _open_sessions_queryset(statuses).count()
    except Exception:
        return 0


def _expire_stale_waiting_sessions(minutes: int = 5) -> None:
    """
    오래된 대기 상담 자동 종료 처리.
    테스트 중 남은 waiting 세션이 계속 상담 요청 목록에 뜨는 문제를 막음.
    """
    fields = _model_fields(LiveChatSession)

    if "status" not in fields:
        return

    now = timezone.now()
    cutoff = now - timedelta(minutes=minutes)

    qs = LiveChatSession.objects.filter(status__in=_waiting_statuses())

    # 이미 종료 시간이 찍힌 세션은 건드리지 않음
    if "ended_at" in fields:
        qs = qs.filter(ended_at__isnull=True)

    if "created_at" in fields:
        qs = qs.filter(created_at__lt=cutoff)
    elif "requested_at" in fields:
        qs = qs.filter(requested_at__lt=cutoff)
    else:
        # 시간 기준 컬럼이 없으면 잘못해서 전체 waiting을 종료할 수 있으니 중단
        return

    update_kwargs: Dict[str, Any] = {
        "status": _status_value("ENDED", "ended"),
    }

    if "ended_at" in fields:
        update_kwargs["ended_at"] = now

    if "updated_at" in fields:
        update_kwargs["updated_at"] = now

    try:
        qs.update(**update_kwargs)
    except Exception:
        log.warning("expire stale waiting sessions failed", exc_info=True)


def _expire_stale_active_sessions(minutes: int = 120) -> None:
    """Close abandoned active sessions so they cannot reappear as new work."""
    fields = _model_fields(LiveChatSession)
    if "status" not in fields:
        return

    now = timezone.now()
    cutoff = now - timedelta(minutes=max(1, minutes))
    qs = LiveChatSession.objects.filter(status__in=_active_statuses())

    if "ended_at" in fields:
        qs = qs.filter(ended_at__isnull=True)

    if "started_at" in fields and "created_at" in fields:
        qs = qs.filter(
            Q(started_at__lt=cutoff)
            | (Q(started_at__isnull=True) & Q(created_at__lt=cutoff))
        )
    elif "started_at" in fields:
        qs = qs.filter(started_at__lt=cutoff)
    elif "created_at" in fields:
        qs = qs.filter(created_at__lt=cutoff)
    else:
        return

    # 수동 종료(api_livechat_end)와 동일하게 "저장 필요" 단계를 거치게 해서
    # 방치된 상담도 상담사가 검토/기록할 기회 없이 콘솔에서 사라지지 않게 한다.
    update_kwargs: Dict[str, Any] = {"status": _status_value("ENDED_NEED_SAVE", "ended_need_save")}
    if "ended_at" in fields:
        update_kwargs["ended_at"] = now
    if "updated_at" in fields:
        update_kwargs["updated_at"] = now

    try:
        qs.update(**update_kwargs)
    except Exception:
        log.warning("expire stale active sessions failed", exc_info=True)


def _expire_stale_console_sessions() -> None:
    _expire_stale_waiting_sessions(
        minutes=_int_setting("LIVECHAT_WAITING_EXPIRE_MINUTES", 30)
    )
    _expire_stale_active_sessions(
        minutes=_int_setting("LIVECHAT_ACTIVE_EXPIRE_MINUTES", 120)
    )


def _console_sessions_queryset():
    """
    상담자 콘솔 왼쪽 목록용 queryset.
    전체 상담 세션이 아니라 현재 볼 필요가 있는 세션만 가져온다.
    """
    fields = _model_fields(LiveChatSession)

    qs = LiveChatSession.objects.all()

    if "status" in fields:
        qs = qs.filter(status__in=_visible_console_statuses())

    if "room" in fields:
        qs = qs.exclude(room__isnull=True).exclude(room__exact="")

    if "ended_at" in fields:
        qs = qs.filter(
            Q(ended_at__isnull=True)
            | Q(status__in=["ended_need_save", "need_note", "saved", "done"])
        )

    # WebSocket/store fallback rows are not customer requests. The request API
    # always records requested_at, so only those rows belong in the queue.
    if "requested_at" in fields:
        qs = qs.filter(requested_at__isnull=False)

    if "created_at" in fields:
        qs = qs.order_by("-created_at")
    elif "started_at" in fields:
        qs = qs.order_by("-started_at")
    else:
        qs = qs.order_by("-id")

    return qs


# ─────────────────────────────────────
#  상담사 콘솔 화면 (어드민용)
# ─────────────────────────────────────
@staff_member_required
@require_GET
def live_chat_view(request: HttpRequest) -> HttpResponse:
    """
    /ragadmin/live-chat/
    """
    fields = _model_fields(LiveChatSession)
    initial_room = request.GET.get("room") or "master"

    # 오래된 waiting 세션 먼저 정리
    _expire_stale_console_sessions()

    # 전체 세션이 아니라 상담 콘솔에 보여줄 세션만 조회
    qs = _console_sessions_queryset()
    sessions = list(qs[:30])

    room_cnt = 0
    if "room" in fields:
        try:
            room_cnt = (
                qs.exclude(room__isnull=True)
                .exclude(room__exact="")
                .values("room")
                .distinct()
                .count()
            )
        except Exception:
            room_cnt = 0

    ctx = {
        "initial_room": initial_room,
        "sessions": sessions,
        "room_cnt": room_cnt,
    }
    return render(request, "ragadmin/live_chat.html", ctx)


# ─────────────────────────────────────
#  상담사용 "다음 상담 받기" (레거시/호환)
# ─────────────────────────────────────
@staff_member_required
@csrf_protect
@require_POST
def api_livechat_next_session(request: HttpRequest) -> JsonResponse:
    """
    (레거시 호환) status 문자열 기반 waiting → active 전환 버전
    """
    if _has_unlogged_session(request.user):
        return JsonResponse(
            {
                "ok": False,
                "reason": "NEED_NOTE",
                "message": "이전 상담에 대한 상담 기록을 먼저 저장해야 다음 상담을 받을 수 있어요.",
            },
            status=400,
        )

    # ✅ 레거시 next API에서도 동시에 진행 중인 상담 수 제한
    max_active = _int_setting("LIVECHAT_MAX_ACTIVE_SESSIONS", 1)
    active_count = _open_session_count(_active_statuses())

    if max_active > 0 and active_count >= max_active:
        return JsonResponse(
            {
                "ok": False,
                "reason": "ACTIVE_LIMIT",
                "message": "이미 진행 중인 상담이 있습니다. 기존 상담을 종료한 뒤 다음 상담을 받을 수 있어요.",
            },
            status=200,
        )

    fields = _model_fields(LiveChatSession)
    qs = LiveChatSession.objects.all()

    if "status" in fields:
        qs = qs.filter(status__in=["waiting", "대기", "pending"])

    if "created_at" in fields:
        qs = qs.order_by("created_at")
    elif "requested_at" in fields:
        qs = qs.order_by("requested_at")
    else:
        qs = qs.order_by("id")

    session = qs.first()
    if not session:
        return JsonResponse(
            {"ok": False, "reason": "NO_WAITING", "message": "현재 대기 중인 상담이 없습니다."},
            status=200,
        )

    now = timezone.now()
    try:
        update_fields: List[str] = []

        if "status" in fields:
            session.status = "active"
            update_fields.append("status")

        if "started_at" in fields and not getattr(session, "started_at", None):
            session.started_at = now
            update_fields.append("started_at")

        if "assigned_staff" in fields and request.user and not request.user.is_anonymous:
            setattr(session, "assigned_staff", request.user)
            update_fields.append("assigned_staff")

        session.save(update_fields=update_fields) if update_fields else session.save()
    except Exception as e:
        log.exception("next-session assign failed: %s", e)
        return JsonResponse(
            {"ok": False, "reason": "ASSIGN_FAILED", "message": "세션 배정 처리 중 오류가 발생했습니다."},
            status=200,
        )

    ts = int(now.timestamp() * 1000)
    _send_master(
        {
            "type": "session_assigned",
            "room": session.room,
            "session_id": session.id,
            "ts": ts,
            "status": getattr(session, "status", None),
        }
    )

    return JsonResponse({"ok": True, "session_id": session.id, "room": session.room, "started_at": getattr(session, "started_at", None)})


@require_POST
@staff_member_required
@csrf_protect
def api_livechat_next(request: HttpRequest) -> JsonResponse:
    """
    (메인) Enum Status 기반 WAITING → ACTIVE 전환 버전
    """

    # 오래된 waiting 세션 먼저 정리
    _expire_stale_console_sessions()

    # 1) ENDED_NEED_SAVE가 남아있으면 막기(가능하면)
    try:
        need_note_exists = LiveChatSession.objects.filter(status=LiveChatSession.Status.ENDED_NEED_SAVE).exists()  # type: ignore[attr-defined]
    except Exception:
        need_note_exists = False

    if need_note_exists or _has_unlogged_session(request.user):
        return JsonResponse(
            {"ok": False, "reason": "NEED_NOTE", "message": "이전에 종료된 상담의 상담 기록을 먼저 저장해 주세요."},
            status=200,
        )

    # ✅ 동시에 진행 가능한 active 상담 수 제한
    max_active = _int_setting("LIVECHAT_MAX_ACTIVE_SESSIONS", 1)
    active_count = _open_session_count(_active_statuses())

    if max_active > 0 and active_count >= max_active:
        return JsonResponse(
            {
                "ok": False,
                "reason": "ACTIVE_LIMIT",
                "message": "이미 진행 중인 상담이 있습니다. 기존 상담을 종료한 뒤 다음 상담을 받을 수 있어요.",
            },
            status=200,
        )

    # 2) WAITING 중 가장 오래된 것
    qs = _open_sessions_queryset(_waiting_statuses())

    fields = _model_fields(LiveChatSession)

    if "room" in fields:
        qs = qs.exclude(room__isnull=True).exclude(room__exact="")

    if "created_at" in fields:
        qs = qs.order_by("created_at")
    elif "requested_at" in fields:
        qs = qs.order_by("requested_at")
    else:
        qs = qs.order_by("id")

    session = qs.first()

    if not session:
        return JsonResponse({"ok": False, "reason": "NO_WAITING", "message": "현재 대기 중인 상담이 없습니다."}, status=200)

    # 3) ACTIVE 전환
    try:
        session.mark_active()
        update_fields = ["status", "started_at"]
    except Exception:
        try:
            session.status = LiveChatSession.Status.ACTIVE  # type: ignore[attr-defined]
        except Exception:
            session.status = "active"
        if not getattr(session, "started_at", None):
            session.started_at = timezone.now()
        update_fields = ["status", "started_at"]

    session.save(update_fields=update_fields)

    return JsonResponse(
        {
            "ok": True,
            "session_id": session.id,
            "room": session.room,
            "status": session.status,
            "page": {"title": getattr(session, "page_title", "") or "", "path": getattr(session, "page_path", "") or ""},
        }
    )


# ─────────────────────────────────────
#  상담 요청 API (QARAG → LiveChat)
# ─────────────────────────────────────
@csrf_exempt
@require_POST
def api_livechat_request(request: HttpRequest) -> JsonResponse:
    """
    /api/livechat/request/
    - 상담 요청이 오면 LiveChatSession을 '무조건 생성'
    - master 로비에 handoff 이벤트 브로드캐스트
    - redirect_url(/c/<room>/)도 함께 내려줌
    """
    data = _json_body(request)
    fields = _model_fields(LiveChatSession)

    # ✅ 오래된 waiting 세션 정리 후, 대기열 제한 확인
    _expire_stale_console_sessions()

    max_waiting = _int_setting("LIVECHAT_MAX_WAITING_SESSIONS", 3)
    waiting_count = _open_session_count(_waiting_statuses())

    if max_waiting > 0 and waiting_count >= max_waiting:
        return JsonResponse(
            {
                "ok": False,
                "reason": "WAITING_FULL",
                "message": "현재 상담 대기 요청이 많습니다. 잠시 후 다시 시도해 주세요.",
            },
            status=200,
        )

    room = (data.get("room") or "").strip()
    if not room:
        # Keep the token non-numeric. Numeric-only room IDs can be mistaken for
        # phone/account numbers by the PII guard when used in URLs or queries.
        token = uuid.uuid4().hex.translate(str.maketrans("0123456789", "abcdefghij"))
        room = "r_" + token[:20]

    now = timezone.now()
    s = LiveChatSession(room=room)
    if "status" in fields:
        s.status = data.get("status") or "waiting"
    if "requested_at" in fields:
        s.requested_at = now

    page = data.get("page") or {}
    if isinstance(page, dict):
        if "page_title" in fields and page.get("title"):
            s.page_title = str(page.get("title"))
        if "page_path" in fields and page.get("path"):
            s.page_path = str(page.get("path"))

    if "code" in fields and data.get("code"):
        s.code = str(data.get("code"))
    if "memo" in fields and data.get("memo"):
        s.memo = str(data.get("memo"))

    try:
        if "source" in fields and data.get("source"):
            s.source = str(data.get("source"))
    except Exception:
        pass
    try:
        if "client_ip" in fields:
            s.client_ip = request.META.get("REMOTE_ADDR") or ""
    except Exception:
        pass

    s.save()

    # ✅ LiveChatRoom row 보장 (실패해도 무시)
    _ensure_livechat_room_row(s.room)

    # ✅ (선택) 초기 system 메시지 2개: DB 컬럼 불일치에도 안전하게 저장
    try:
        now_ts = int(now.timestamp() * 1000)
        save_ws_message(room=s.room, session_id=s.id, sender_norm="system", effective_type="system", body="욕설 폭언 모욕적인 언행 발견시 즉시 상담 종료되니 주의해주세요.", ts=now_ts)
        save_ws_message(room=s.room, session_id=s.id, sender_norm="system", effective_type="system", body="오늘 하루도 좋은 하루 보내시길 바랍니다.", ts=now_ts + 1)
    except Exception:
        # 시스템 메시지 실패는 절대 전체 플로우를 막지 않음
        log.warning("initial system messages skipped", exc_info=True)

    # ✅ 상담 전용 페이지 URL
    try:
        redirect_path = f"/c/{s.room}/"
        redirect_url = request.build_absolute_uri(redirect_path)
    except Exception:
        redirect_url = f"/c/{s.room}/"

    now_ts = int(now.timestamp() * 1000)
    _send_master(
        {
            "type": "session_created",
            "room": s.room,
            "session_id": s.id,
            "status": getattr(s, "status", None),
            "ts": now_ts,
            "page": {"title": getattr(s, "page_title", "") or "", "path": getattr(s, "page_path", "") or ""},
        }
    )
    _send_master(
        {
            "type": "handoff",
            "room": s.room,
            "session_id": s.id,
            "ts": now_ts,
            "page": {"title": getattr(s, "page_title", "") or "", "path": getattr(s, "page_path", "") or ""},
            "url": request.headers.get("Referer") or "",
        }
    )

    return JsonResponse({"ok": True, "room": s.room, "session_id": s.id, "redirect_url": redirect_url})


# ─────────────────────────────────────
#  상담 내역 조회 API  (✅ store.fetch 기반 “무조건 안전 조회”)
# ─────────────────────────────────────
@require_GET
def api_livechat_history(request: HttpRequest) -> JsonResponse:
    """
    /api/livechat/history/?session_id=...&limit=...
    /api/livechat/history/?room=...&limit=...
    옵션: before_ts=... (pagination)
    """
    if not getattr(settings, "LIVECHAT_PERSIST_MESSAGES", True):
        return JsonResponse({"ok": True, "messages": []})

    session_id = _to_int(request.GET.get("session_id") or request.GET.get("sid"))
    room = (request.GET.get("room") or "").strip()
    limit = _to_int(request.GET.get("limit")) or 200
    limit = max(1, min(limit, 500))
    before_ts = _ts_ms(request.GET.get("before_ts"))

    # 1) 세션 찾기
    sess: Optional[LiveChatSession] = None
    if session_id:
        sess = LiveChatSession.objects.filter(id=session_id).first()
    if sess is None and room:
        sess = LiveChatSession.objects.filter(room=room).order_by("-id").first()

    if sess is None:
        return JsonResponse({"ok": True, "room": room, "session_id": session_id, "messages": []})

    # 2) 접근 제어(최소)
    is_staff = bool(getattr(request.user, "is_authenticated", False) and getattr(request.user, "is_staff", False))
    if not is_staff:
        # 비로그인은 room 토큰이 일치해야만 조회 허용
        if not room or str(getattr(sess, "room", "")) != room:
            return JsonResponse({"ok": False, "error": "forbidden"}, status=403)

    # 3) ✅ 단일 진실: store.fetch_session_messages_for_log
    rows = fetch_session_messages_for_log(session_id=int(sess.id), limit=5000)

    def _row_ts_ms(r: Dict[str, Any]) -> int:
        ca = r.get("created_at")
        if ca is not None:
            try:
                return int(ca.timestamp() * 1000)
            except Exception:
                pass
        return int(timezone.now().timestamp() * 1000)

    if before_ts:
        rows = [r for r in rows if _row_ts_ms(r) < int(before_ts)]

    if len(rows) > limit:
        rows = rows[-limit:]  # 최신 limit개

    messages: List[Dict[str, Any]] = []
    for r in rows:
        role = str((r.get("role") or "system")).strip().lower()
        if role not in ("user", "operator", "system"):
            role = "system"

        # fetch는 text 키가 기본이지만, 템플릿/호환 위해 content도 같이 봄
        text = r.get("text")
        if text is None:
            text = r.get("content", "")

        ts = _row_ts_ms(r)

        payload: Dict[str, Any] = {
            "type": "message",
            "sender": role,
            "text": str(text or ""),
            "ts": ts,
            "room": str(getattr(sess, "room", room) or room),
            "session_id": int(getattr(sess, "id", session_id) or 0),
        }

        # ✅ staff면 retention 메타도 같이 내려주면 디버깅/운영에 도움됨
        if is_staff:
            if "retention_class" in r:
                payload["retention_class"] = r.get("retention_class")
            if "purge_at" in r and r.get("purge_at") is not None:
                try:
                    payload["purge_at"] = r["purge_at"].isoformat()
                except Exception:
                    payload["purge_at"] = str(r.get("purge_at"))
            if "flagged_at" in r and r.get("flagged_at") is not None:
                try:
                    payload["flagged_at"] = r["flagged_at"].isoformat()
                except Exception:
                    payload["flagged_at"] = str(r.get("flagged_at"))
            if "flag_reason" in r:
                payload["flag_reason"] = r.get("flag_reason", "")
            if "abuse_flagged" in r:
                payload["abuse_flagged"] = bool(r.get("abuse_flagged", False))

        messages.append(payload)

    closed = _is_livechat_closed(sess)

    return JsonResponse(
        {
            "ok": True,
            "room": str(getattr(sess, "room", room) or room),
            "session_id": int(getattr(sess, "id", session_id) or 0),
            "status": getattr(sess, "status", None),
            "started_at": getattr(sess, "started_at", None).isoformat() if getattr(sess, "started_at", None) else None,
            "ended_at": getattr(sess, "ended_at", None).isoformat() if getattr(sess, "ended_at", None) else None,
            "closed": closed,
            "can_send": not closed,
            "messages": messages,
        }
    )


# ─────────────────────────────────────
#  상담 저장 API  (/api/save/)
# ─────────────────────────────────────
@require_POST
@staff_member_required
@csrf_protect
def api_livechat_save(request: HttpRequest) -> JsonResponse:
    """
    상담 종료 후, 상담사 콘솔에서 남긴 '상담 기록'을 저장하는 API.
    """
    data = _json_body(request)

    sid = _to_int(data.get("session_id"))
    room = (data.get("room") or "").strip()

    if not sid and not room:
        return JsonResponse({"ok": False, "message": "session_id 또는 room 중 하나는 반드시 포함되어야 합니다."}, status=400)

    qs = LiveChatSession.objects.all()
    obj: Optional[LiveChatSession] = None
    if sid:
        obj = qs.filter(id=sid).first()
    if obj is None and room:
        obj = qs.filter(room=room).order_by("-id").first()

    if obj is None:
        return JsonResponse({"ok": False, "message": "대상 상담 세션을 찾을 수 없습니다."}, status=404)

    fields = _model_fields(LiveChatSession)
    now = timezone.now()

    session_type = (data.get("session_type") or "").strip()
    session_note = (data.get("session_note") or "").strip()
    session_detail = (data.get("session_detail") or "").strip()

    try:
        if "session_type" in fields:
            setattr(obj, "session_type", session_type or None)
        if "session_note" in fields:
            setattr(obj, "session_note", session_note or None)
        if "session_detail" in fields:
            setattr(obj, "session_detail", session_detail or None)

        if "memo" in fields and (session_note or session_detail):
            if not getattr(obj, "memo", ""):
                obj.memo = session_note or session_detail

        if "processed_at" in fields:
            obj.processed_at = now
        if "ended_at" in fields and not getattr(obj, "ended_at", None):
            obj.ended_at = now

        mark_saved = getattr(obj, "mark_saved", None)
        if callable(mark_saved):
            mark_saved()
        elif "status" in fields:
            Status = getattr(LiveChatSession, "Status", None)
            if Status is not None and hasattr(Status, "SAVED"):
                obj.status = Status.SAVED  # type: ignore[attr-defined]
            else:
                obj.status = "saved"

        obj.save()
    except Exception as e:
        log.exception("livechat api_livechat_save failed: %s", e)
        return JsonResponse({"ok": False, "message": "상담 기록 저장 중 서버 오류가 발생했습니다."}, status=500)

    try:
        _send_master(
            {
                "type": "session_saved",
                "room": getattr(obj, "room", None),
                "session_id": obj.id,
                "status": getattr(obj, "status", None),
                "ts": int(now.timestamp() * 1000),
            }
        )
    except Exception:
        pass

    return JsonResponse(
        {
            "ok": True,
            "message": "상담 기록을 저장했습니다.",
            "session_id": obj.id,
            "room": getattr(obj, "room", None),
            "session_status": getattr(obj, "status", None),
            "status_after": getattr(obj, "status", None),
        }
    )


# ─────────────────────────────────────
#  상담 종료 API
# ─────────────────────────────────────
def _get_session_by_sid_or_room(sid: Optional[int], room: str) -> Tuple[Optional[LiveChatSession], Set[str]]:
    qs = LiveChatSession.objects.all()
    fields = _model_fields(LiveChatSession)
    obj: Optional[LiveChatSession] = None
    if sid:
        obj = qs.filter(id=sid).order_by("-id").first()
    if obj is None and room:
        obj = qs.filter(room=room).order_by("-id").first()
    return obj, fields


@require_POST
@staff_member_required
@csrf_protect
def api_livechat_end(request: HttpRequest) -> JsonResponse:
    """
    상담사가 콘솔에서 '상담 종료' 버튼 눌렀을 때 호출되는 API.
    """
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return JsonResponse({"ok": False, "message": "잘못된 JSON 입니다."}, status=400)

    sid = _to_int(payload.get("session_id"))
    room = (payload.get("room") or "").strip()
    end_text = END_MESSAGE

    obj, fields = _get_session_by_sid_or_room(sid, room)
    if obj is None:
        return JsonResponse({"ok": False, "message": "세션을 찾을 수 없습니다."}, status=404)

    now = timezone.now()
    changed_fields: List[str] = []

    try:
        used_mark_method = False

        mark_ended_need_save = getattr(obj, "mark_ended_need_save", None)
        if callable(mark_ended_need_save):
            mark_ended_need_save()
            used_mark_method = True
        else:
            if "status" in fields:
                Status = getattr(LiveChatSession, "Status", None)
                if Status is not None and hasattr(Status, "ENDED_NEED_SAVE"):
                    obj.status = Status.ENDED_NEED_SAVE  # type: ignore[attr-defined]
                else:
                    obj.status = "ended"
                changed_fields.append("status")

        if "ended_at" in fields and not getattr(obj, "ended_at", None):
            obj.ended_at = now
            changed_fields.append("ended_at")

        if used_mark_method:
            obj.save()
        else:
            obj.save(update_fields=changed_fields) if changed_fields else obj.save()

    except Exception:
        log.exception("api_livechat_end: session status update failed")

    ts = int(now.timestamp() * 1000)
    room_name = getattr(obj, "room", room)

    _ensure_livechat_room_row(room_name)

    try:
        _send_room(
            room_name,
            {
                "type": "end",
                "event": "session_closed",
                "closed": True,
                "can_send": False,
                "sender": "operator",
                "text": end_text,
                "ts": ts,
                "room": room_name,
                "session_id": getattr(obj, "id", None),
            },
        )
    except Exception:
        log.exception("api_livechat_end: room broadcast failed")

    # ✅ (추가) 종료 멘트도 DB에 저장
    try:
        save_ws_message(
            room=room_name,
            session_id=getattr(obj, "id", None),
            sender_norm="operator",
            effective_type="end",
            body=end_text,
            ts=ts,
        )
    except Exception:
        pass

    try:
        _send_master(
            {"type": "session_ended", "room": room_name, "session_id": getattr(obj, "id", None), "status": getattr(obj, "status", None), "ended_at": getattr(obj, "ended_at", None), "ts": ts}
        )
    except Exception:
        log.exception("api_livechat_end: master broadcast failed")

    return JsonResponse({"ok": True, "message": "상담을 종료했습니다.", "session_id": getattr(obj, "id", None), "room": room_name, "session_status": getattr(obj, "status", None)})


@require_POST
@csrf_protect
def api_livechat_client_end(request: HttpRequest, room: str) -> JsonResponse:
    """
    고객(비로그인) 전용 상담 종료 API.
    """
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = {}

    end_text = END_MESSAGE

    obj, fields = _get_session_by_sid_or_room(None, (room or "").strip())
    if obj is None:
        return JsonResponse({"ok": False, "message": "세션을 찾을 수 없습니다."}, status=404)

    now = timezone.now()
    changed_fields: List[str] = []

    try:
        used_mark_method = False

        mark_ended_need_save = getattr(obj, "mark_ended_need_save", None)
        if callable(mark_ended_need_save):
            mark_ended_need_save()
            used_mark_method = True
        else:
            if "status" in fields:
                Status = getattr(LiveChatSession, "Status", None)
                if Status is not None and hasattr(Status, "ENDED_NEED_SAVE"):
                    obj.status = Status.ENDED_NEED_SAVE  # type: ignore[attr-defined]
                else:
                    obj.status = "ended"
                changed_fields.append("status")

        if "ended_at" in fields and not getattr(obj, "ended_at", None):
            obj.ended_at = now
            changed_fields.append("ended_at")

        if used_mark_method:
            obj.save()
        else:
            obj.save(update_fields=changed_fields) if changed_fields else obj.save()

    except Exception:
        log.exception("api_livechat_end: session status update failed")

    ts = int(now.timestamp() * 1000)
    room_name = getattr(obj, "room", room)

    _ensure_livechat_room_row(room_name)

    try:
        _send_room(
            room_name,
            {
                "type": "end",
                "event": "session_closed",
                "closed": True,
                "can_send": False,
                "sender": "operator",
                "role": "operator",
                "text": end_text,
                "ts": ts,
                "room": room_name,
                "session_id": getattr(obj, "id", None),
            },
        )
    except Exception:
        log.exception("api_livechat_client_end: room broadcast failed")

    # ✅ (추가) 클라이언트 종료 멘트도 DB에 저장
    try:
        save_ws_message(
            room=room_name,
            session_id=getattr(obj, "id", None),
            sender_norm="operator",
            effective_type="end",
            body=end_text,
            ts=ts,
        )
    except Exception:
        pass

    try:
        _send_master(
            {"type": "session_ended", "room": room_name, "session_id": getattr(obj, "id", None), "status": getattr(obj, "status", None), "ended_at": getattr(obj, "ended_at", None), "ts": ts, "by": "client"}
        )
    except Exception:
        log.exception("api_livechat_client_end: master broadcast failed")

    return JsonResponse({"ok": True, "message": "상담을 종료했습니다.", "session_id": getattr(obj, "id", None), "room": room_name, "session_status": getattr(obj, "status", None)})


# ─────────────────────────────────────
#  최근 세션/저장/정리 (운영자용)
# ─────────────────────────────────────
@staff_member_required
@require_GET
def api_livechat_recent_sessions(request: HttpRequest) -> HttpResponse:
    """
    /api/livechat/recent-sessions/
    """
    # 오래된 waiting 세션 먼저 정리
    _expire_stale_console_sessions()

    # 전체 세션이 아니라 상담 콘솔에 보여줄 세션만 조회
    qs = _console_sessions_queryset()
    sessions = list(qs[:30])

    cleanup_url = "/ragadmin/live-chat/cleanup/"
    html = render_to_string(
        "ragadmin/_live_chat_session_items.html",
        {"sessions": sessions, "cleanup_url": cleanup_url},
        request=request,
    )

    accept = (request.headers.get("Accept") or "").lower()
    want_json = ("application/json" in accept) or (request.GET.get("format") == "json")
    if want_json:
        return JsonResponse({"ok": True, "html": html})
    return HttpResponse(html)


@staff_member_required
@csrf_protect
@require_POST
def live_chat_save_session_view(request: HttpRequest) -> JsonResponse:
    """
    /api/livechat/save-session/
    """
    data = _json_body(request)
    session_id = str(data.get("session_id") or "").strip()
    room = str(data.get("room") or "").strip()

    s: Optional[LiveChatSession] = None
    if session_id.isdigit():
        s = LiveChatSession.objects.filter(id=int(session_id)).first()
    if not s and room:
        s = LiveChatSession.objects.filter(room=room).order_by("-id").first()
    if not s:
        return JsonResponse({"ok": False, "error": "session_not_found"}, status=200)

    fields = _model_fields(LiveChatSession)
    now = timezone.now()

    stype = str(data.get("session_type") or "").strip()
    snote = str(data.get("session_note") or "").strip()
    sdetail = str(data.get("session_detail") or "").strip()

    try:
        if "session_type" in fields and stype:
            s.session_type = stype
        if "session_note" in fields:
            s.session_note = snote or None  # type: ignore[attr-defined]
        if "session_detail" in fields:
            s.session_detail = sdetail or None  # type: ignore[attr-defined]
        if "memo" in fields and (sdetail or snote) and not getattr(s, "memo", ""):
            s.memo = sdetail or snote
        if "processed_at" in fields:
            s.processed_at = now
        if "ended_at" in fields and not getattr(s, "ended_at", None):
            s.ended_at = now
        if "status" in fields:
            s.status = "done"
        s.save()
    except Exception as e:
        log.exception("save-session failed: %s", e)
        return JsonResponse({"ok": False, "error": "save_failed"}, status=200)

    _send_master({"type": "session_saved", "room": s.room, "session_id": s.id, "ts": int(now.timestamp() * 1000)})
    return JsonResponse({"ok": True, "session_id": s.id})


@staff_member_required
@csrf_protect
@require_POST
def live_chat_cleanup_view(request: HttpRequest) -> JsonResponse:
    """
    /ragadmin/live-chat/cleanup/
    """
    data = _json_body(request)
    fields = _model_fields(LiveChatSession)
    now = timezone.now()

    session_id = str(data.get("session_id") or "").strip()
    if session_id.isdigit():
        sid = int(session_id)
        try:
            LiveChatSession.objects.filter(id=sid).delete()
        except Exception as e:
            log.warning("cleanup delete failed: %s", e)
            return JsonResponse({"ok": False, "error": "delete_failed"}, status=200)

        _send_master({"type": "session_deleted", "session_id": sid, "ts": int(now.timestamp() * 1000)})
        return JsonResponse({"ok": True})

    mode = str(data.get("mode") or "").strip()
    if mode == "today":
        qs = LiveChatSession.objects.all()
        today = timezone.localdate()
        if "created_at" in fields:
            qs = qs.filter(created_at__date=today)

        if "status" in fields:
            exclude_statuses = ["ended", "done", "종료"]
            try:
                Status = LiveChatSession.Status  # type: ignore[attr-defined]
                for name in ("SAVED", "ENDED_NEED_SAVE"):
                    if hasattr(Status, name):
                        v = getattr(Status, name)
                        exclude_statuses.append(getattr(v, "value", v))
            except Exception:
                pass
            qs = qs.exclude(status__in=exclude_statuses)

        update_kwargs: Dict[str, Any] = {}
        if "status" in fields:
            update_kwargs["status"] = "ended"
        if "ended_at" in fields:
            update_kwargs["ended_at"] = now

        try:
            if update_kwargs:
                qs.update(**update_kwargs)
        except Exception as e:
            log.warning("cleanup today update failed: %s", e)
            return JsonResponse({"ok": False, "error": "cleanup_failed"}, status=200)

        _send_master({"type": "sessions_cleaned", "ts": int(now.timestamp() * 1000)})
        return JsonResponse({"ok": True})

    return JsonResponse({"ok": False, "error": "bad_request"}, status=200)


# ─────────────────────────────────────
#  상담 전용 클라이언트 페이지 (/c/<room>/)
# ─────────────────────────────────────
@require_GET
@ensure_csrf_cookie
def livechat_client_room_view(request: HttpRequest, room: str) -> HttpResponse:
    session = LiveChatSession.objects.filter(room=room).order_by("-id").first()
    if not session:
        raise Http404("유효하지 않은 상담 세션입니다.")

    ctx = {
        "session": session,
        "room": getattr(session, "room", room),
        "room_token": getattr(session, "room", room),
        "session_id": getattr(session, "id", None),
        "SERVICE_NAME": getattr(settings, "SERVICE_NAME", "김동건 포트폴리오"),
        "LIVECHAT_END_URL": reverse("livechat:api_livechat_client_end", kwargs={"room": room}),
    }
    return render(request, "ragapp/livechat/client_room.html", ctx)


# ─────────────────────────────────────
#  채팅기록 확인 (✅ store.fetch로 “무조건 보이게”)
# ─────────────────────────────────────
@staff_member_required
@require_GET
def livechat_session_log_view(request: HttpRequest, session_id: int) -> HttpResponse:
    session = LiveChatSession.objects.filter(id=session_id).first()
    if not session:
        raise Http404("해당 상담 세션을 찾을 수 없습니다.")

    message_items = fetch_session_messages_for_log(session_id=session.id, limit=5000)

    # ✅ 템플릿이 m.content를 봐도 무조건 보이게
    for it in message_items:
        if "content" not in it:
            it["content"] = it.get("text", "")  # dict니까 템플릿에서 m.content 가능

    ctx = {
        "session": session,
        "message_items": message_items,
        "SERVICE_NAME": getattr(settings, "SERVICE_NAME", "김동건 포트폴리오"),
    }
    return render(request, "ragadmin/live_chat_session_log.html", ctx)


# ─────────────────────────────────────
#  간단 가용성 체크 (예전용)
# ─────────────────────────────────────
@require_GET
def livechat_availability_api(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True, "available": True})


# ─────────────────────────────────────
# 레거시 이름 호환
# ─────────────────────────────────────
livechat_request_api = api_livechat_request
live_chat_recent_sessions = api_livechat_recent_sessions
livechat_recent_sessions_view = api_livechat_recent_sessions
livechat_next_session_api = api_livechat_next_session
