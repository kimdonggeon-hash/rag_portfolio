# ragapp/livechat/views.py
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple, Set

from django.apps import apps
from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_GET, require_POST

from ragapp.models import LiveChatSession

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
    try:
        return {f.name for f in model._meta.get_fields() if hasattr(f, "attname")}
    except Exception:
        return set()


def _to_int(v: Any) -> Optional[int]:
    """
    session_id 같이 들어오는 값들을 int로 정리하기 위한 헬퍼.
    - 숫자 문자열이면 int로, 아니면 None
    """
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


def _get_message_model():
    """
    메시지 모델을 유연하게 찾기 위한 헬퍼.
    - LiveChatMessage / LiveChatMsg / ChatMessage / LiveChatLog 중 하나를 ragapp에서 찾음
    """
    for name in ("LiveChatMessage", "LiveChatMsg", "ChatMessage", "LiveChatLog"):
        try:
            m = apps.get_model("ragapp", name)
            if m:
                return m
        except Exception:
            continue
    return None


def _has_unlogged_session(user) -> bool:
    """
    아직 '상담 기록'을 남기지 않은 종료 세션이 있는지 체크.
    """
    fields = _model_fields(LiveChatSession)

    # 기록으로 쓸 필드가 아예 없으면 이 기능은 비활성화
    note_fields = [f for f in ("session_note", "session_detail") if f in fields]
    if not note_fields:
        return False

    qs = LiveChatSession.objects.all()

    # 상담사별로 나눠 관리하는 경우
    if "assigned_staff" in fields and user and not getattr(user, "is_anonymous", False):
        qs = qs.filter(assigned_staff=user)

    # ✅ 이미 "완료/저장됨" 상태인 건 제외
    if "status" in fields:
        done_values = ["done"]  # 옛날 문자열
        try:
            # enum 기반 상태값(SAVED)이 있다면 같이 제외
            done_values.append(LiveChatSession.Status.SAVED)
        except Exception:
            pass
        qs = qs.exclude(status__in=done_values)

    # 종료된 세션만 대상으로 (ended_at 있으면 우선)
    if "ended_at" in fields:
        qs = qs.filter(ended_at__isnull=False)

    # 기록이 모두 비어 있는 세션만 남기기
    q = Q()
    for idx, fname in enumerate(note_fields):
        cond = Q(**{f"{fname}__isnull": True}) | Q(**{f"{fname}__exact": ""})
        if idx == 0:
            q = cond
        else:
            q &= cond
    qs = qs.filter(q)

    return qs.exists()


# ─────────────────────────────────────
#  상담사 콘솔 화면 (어드민용)
# ─────────────────────────────────────
@staff_member_required
@require_GET
def live_chat_view(request: HttpRequest) -> HttpResponse:
    """
    /ragadmin/live-chat/
    - 상담사 콘솔 메인 화면
    - 최근 세션 30개까지 전달
    """
    fields = _model_fields(LiveChatSession)
    initial_room = request.GET.get("room") or "master"

    qs = LiveChatSession.objects.all()

    # 최신순(우선 created_at)
    if "created_at" in fields:
        qs = qs.order_by("-created_at")
    elif "started_at" in fields:
        qs = qs.order_by("-started_at")
    else:
        qs = qs.order_by("-id")

    sessions = list(qs[:30])
    ctx = {
        "initial_room": initial_room,
        "sessions": sessions,
    }
    return render(request, "ragadmin/live_chat.html", ctx)


# ─────────────────────────────────────
#  상담사용 "다음 상담 받기" API
# ─────────────────────────────────────
@staff_member_required
@csrf_protect
@require_POST
def api_livechat_next_session(request: HttpRequest) -> JsonResponse:
    """
    /ragadmin/live-chat/next-session/ (예정)
    - 상담사 콘솔의 '다음 상담 받기' 버튼에서 호출한다고 가정.
    - 아직 상담 기록(session_note/session_detail)을 남기지 않은
      종료 세션이 하나라도 있으면 다음 배정을 막는다.
    """
    # 1) 아직 기록 안 남긴 세션이 있으면 차단
    if _has_unlogged_session(request.user):
        return JsonResponse(
            {
                "ok": False,
                "reason": "NEED_NOTE",
                "message": "이전 상담에 대한 상담 기록을 먼저 저장해야 다음 상담을 받을 수 있어요.",
            },
            status=400,
        )

    fields = _model_fields(LiveChatSession)
    qs = LiveChatSession.objects.all()

    # '대기중' 상태만 후보로 사용 (status 필드가 있을 때)
    if "status" in fields:
        qs = qs.filter(status__in=["waiting", "대기", "pending"])

    # 가장 오래된 것부터 배정
    if "created_at" in fields:
        qs = qs.order_by("created_at")
    elif "requested_at" in fields:
        qs = qs.order_by("requested_at")
    else:
        qs = qs.order_by("id")

    session = qs.first()
    if not session:
        return JsonResponse(
            {
                "ok": False,
                "reason": "NO_WAITING",
                "message": "현재 대기 중인 상담이 없습니다.",
            },
            status=200,
        )

    now = timezone.now()

    try:
        update_fields: list[str] = []

        if "status" in fields:
            session.status = "active"
            update_fields.append("status")

        if "started_at" in fields and not getattr(session, "started_at", None):
            session.started_at = now
            update_fields.append("started_at")

        if "assigned_staff" in fields and request.user and not request.user.is_anonymous:
            setattr(session, "assigned_staff", request.user)
            update_fields.append("assigned_staff")

        if update_fields:
            session.save(update_fields=update_fields)
        else:
            session.save()
    except Exception as e:
        log.exception("next-session assign failed: %s", e)
        return JsonResponse(
            {
                "ok": False,
                "reason": "ASSIGN_FAILED",
                "message": "세션 배정 처리 중 오류가 발생했습니다.",
            },
            status=200,
        )

    ts = int(now.timestamp() * 1000)

    # 마스터 콘솔에 배정 이벤트 브로드캐스트(선택)
    _send_master(
        {
            "type": "session_assigned",
            "room": session.room,
            "session_id": session.id,
            "ts": ts,
            "status": getattr(session, "status", None),
        }
    )

    return JsonResponse(
        {
            "ok": True,
            "session_id": session.id,
            "room": session.room,
            "started_at": getattr(session, "started_at", None),
        }
    )


@require_POST
@staff_member_required
@csrf_protect
def api_livechat_next(request):
    """
    상담사 콘솔 우측 상단 '다음 상담 받기' 버튼용 API.

    - ENDED_NEED_SAVE 상태인 세션이 하나라도 있으면
      → ok=False, reason=NEED_NOTE 로 돌려보내서
        "상담 기록 먼저 저장해 주세요" 경고를 띄우게 함.
    - 그 외에는 WAITING 중에서 가장 오래된 세션 1개를 ACTIVE 로 전환하고 리턴.
    """
    # 1) 메모 필요 세션이 남아 있는지 확인
    need_note_exists = LiveChatSession.objects.filter(
        status=LiveChatSession.Status.ENDED_NEED_SAVE
    ).exists()

    if need_note_exists:
        return JsonResponse(
            {
                "ok": False,
                "reason": "NEED_NOTE",
                "message": "이전에 종료된 상담의 상담 기록을 먼저 저장해 주세요.",
            },
            status=200,
        )

    # 2) WAITING(대기) 중에서 가장 오래된 하나 선택
    session = (
        LiveChatSession.objects.filter(status=LiveChatSession.Status.WAITING)
        .order_by("created_at")
        .first()
    )

    if not session:
        return JsonResponse(
            {
                "ok": False,
                "reason": "NO_WAITING",
                "message": "현재 대기 중인 상담이 없습니다.",
            },
            status=200,
        )

    # 3) ACTIVE 로 전환 (mark_active() 있으면 우선 사용)
    try:
        session.mark_active()
        update_fields = ["status", "started_at"]
    except Exception:
        session.status = LiveChatSession.Status.ACTIVE
        if not session.started_at:
            session.started_at = timezone.now()
        update_fields = ["status", "started_at"]

    session.save(update_fields=update_fields)

    # 4) 프런트에서 필요한 정보만 리턴
    return JsonResponse(
        {
            "ok": True,
            "session_id": session.id,
            "room": session.room,
            "status": session.status,
            "page": {
                "title": session.page_title or "",
                "path": session.page_path or "",
            },
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
    - 리뉴얼: redirect_url(/c/<room>/)도 함께 내려줌
    """
    data = _json_body(request)
    fields = _model_fields(LiveChatSession)

    room = (data.get("room") or "").strip()
    if not room:
        # room이 없으면 임의 생성
        room = timezone.now().strftime("r%y%m%d%H%M%S%f")[-14:]

    s = LiveChatSession(room=room)
    if "status" in fields:
        s.status = data.get("status") or "waiting"

    page = data.get("page") or {}
    if isinstance(page, dict):
        if "page_title" in fields and page.get("title"):
            s.page_title = str(page.get("title"))
        if "page_path" in fields and page.get("path"):
            s.page_path = str(page.get("path"))

    # code / memo 등은 선택
    if "code" in fields and data.get("code"):
        s.code = str(data.get("code"))
    if "memo" in fields and data.get("memo"):
        s.memo = str(data.get("memo"))

    # (선택) source / client_ip 같은 필드가 있으면 채워주기
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

    # ✅ 상담사 연결 대기 안내 메시지를 세션 첫 메시지 2개로 남기기
    try:
        # LiveChatMessage 위치를 최대한 유연하게 찾음
        MsgModel = None
        try:
            from ragapp.models_chat_retention import LiveChatMessage as _Msg  # type: ignore
            MsgModel = _Msg
        except Exception:
            try:
                from ragapp.models import LiveChatMessage as _Msg  # type: ignore
                MsgModel = _Msg
            except Exception:
                MsgModel = None

        if MsgModel is not None:
            m_fields = _model_fields(MsgModel)

            def _create_system_message(text: str) -> None:
                msg_kwargs: Dict[str, Any] = {}

                # 세션 FK / session_id
                if "session" in m_fields:
                    msg_kwargs["session"] = s
                elif "session_id" in m_fields:
                    msg_kwargs["session_id"] = s.id

                # room 필드가 있으면 같이 기록
                if "room" in m_fields:
                    msg_kwargs["room"] = s.room

                # role / sender
                if "role" in m_fields:
                    msg_kwargs["role"] = "system"
                if "sender" in m_fields:
                    msg_kwargs["sender"] = "system"

                # 본문(content / text / body 등)
                if "content" in m_fields:
                    msg_kwargs["content"] = text
                elif "text" in m_fields:
                    msg_kwargs["text"] = text
                elif "body" in m_fields:
                    msg_kwargs["body"] = text

                # msg_type / type 필드가 있으면 system 타입으로
                if "msg_type" in m_fields:
                    msg_kwargs["msg_type"] = "system"
                elif "type" in m_fields:
                    msg_kwargs["type"] = "system"

                # ts 필드 있으면 ms 기준 타임스탬프
                if "ts" in m_fields:
                    msg_kwargs["ts"] = int(timezone.now().timestamp() * 1000)

                MsgModel.objects.create(**msg_kwargs)

            # 👉 여기서 실제로 두 줄 생성
            _create_system_message("욕설 폭언 모욕적인 언행 발견시 즉시 상담 종료하고 보고 바랍니다.")
            _create_system_message("오늘 하루도 좋은 하루 보내시길 바랍니다.")

        else:
            log.info("livechat initial system messages skipped: no LiveChatMessage model")

    except Exception:
        # 실패해도 전체 플로우는 유지
        log.warning("livechat initial system messages failed", exc_info=True)

    # ✅ 상담 전용 페이지 URL
    try:
        redirect_path = f"/c/{s.room}/"
        redirect_url = request.build_absolute_uri(redirect_path)
    except Exception:
        redirect_path = f"/c/{s.room}/"
        redirect_url = redirect_path

    # master 브로드캐스트 (기존 그대로)
    now_ts = int(timezone.now().timestamp() * 1000)
    _send_master(
        {
            "type": "session_created",
            "room": s.room,
            "session_id": s.id,
            "status": getattr(s, "status", None),
            "ts": now_ts,
            "page": {
                "title": getattr(s, "page_title", "") or "",
                "path": getattr(s, "page_path", "") or "",
            },
        }
    )

    payload = {
        "type": "handoff",
        "room": s.room,
        "session_id": s.id,
        "ts": now_ts,
        "page": {
            "title": getattr(s, "page_title", "") or "",
            "path": getattr(s, "page_path", "") or "",
        },
        "url": request.headers.get("Referer") or "",
    }
    _send_master(payload)

    # ✅ 클라이언트에서 쓸 정보들
    return JsonResponse(
        {
            "ok": True,
            "room": s.room,
            "session_id": s.id,
            "redirect_url": redirect_url,  # QARAG → /c/<room>/ 이동용
        }
    )



# ─────────────────────────────────────
#  상담 내역 조회 API
# ─────────────────────────────────────
@require_GET
def api_livechat_history(request: HttpRequest) -> JsonResponse:
    """
    /api/livechat/history/?session_id=...&limit=...
    /api/livechat/history/?room=...&limit=...

    - session_id가 우선
    - room만 오는 경우: LiveChatSession에서 room으로 세션 찾고 해당 session_id로 메시지 조회
    """
    MessageModel = _get_message_model()
    if not MessageModel:
        return JsonResponse(
            {"ok": False, "error": "message_model_not_enabled"},
            status=200,
        )

    session_id = (request.GET.get("session_id") or "").strip()
    room = (request.GET.get("room") or "").strip()
    limit = int((request.GET.get("limit") or "200").strip() or "200")
    limit = max(1, min(limit, 2000))

    sid: Optional[int] = None
    if session_id.isdigit():
        sid = int(session_id)

    if not sid and room:
        # room 파라미터 지원: room으로 LiveChatSession 조회 → session_id 결정
        try:
            qs = LiveChatSession.objects.filter(room=room).order_by("-id")
            s = qs.first()
            if s:
                sid = s.id
        except Exception as e:
            log.warning("history room->session lookup failed: %s", e)

    if not sid:
        return JsonResponse({"ok": True, "session_id": None, "messages": []})

    try:
        qs = MessageModel.objects.filter(session_id=sid).order_by("-created_at")[:limit]
        items = list(reversed(list(qs)))
    except Exception as e:
        log.exception("history query failed: %s", e)
        return JsonResponse(
            {"ok": False, "error": "history_query_failed"},
            status=200,
        )

    messages = []
    for m in items:
        messages.append(
            {
                "role": getattr(m, "role", "") or "",
                "content": getattr(m, "content", "") or "",
                "created_at": getattr(m, "created_at", None),
            }
        )

    return JsonResponse({"ok": True, "session_id": sid, "messages": messages})


# ─────────────────────────────────────
#  상담 저장 API  (/api/save/)
# ─────────────────────────────────────
@require_POST
@staff_member_required
@csrf_protect
def api_livechat_save(request: HttpRequest) -> JsonResponse:
    """
    상담 종료 후, 상담사 콘솔에서 남긴 '상담 기록'을 저장하는 API.

    기대 요청(JSON):
    {
      "session_id": 123,   # 또는 room
      "room": "r-xxxxx",
      "session_type": "상담 유형",
      "session_note": "한 줄 요약",
      "session_detail": "상세 기록"
    }
    """
    data = _json_body(request)

    # 1) 세션 찾기
    sid = _to_int(data.get("session_id"))
    room = (data.get("room") or "").strip()

    if not sid and not room:
        return JsonResponse(
            {"ok": False, "message": "session_id 또는 room 중 하나는 반드시 포함되어야 합니다."},
            status=400,
        )

    qs = LiveChatSession.objects.all()
    obj: Optional[LiveChatSession] = None

    if sid:
        obj = qs.filter(id=sid).first()
    if obj is None and room:
        obj = qs.filter(room=room).order_by("-id").first()

    if obj is None:
        return JsonResponse(
            {"ok": False, "message": "대상 상담 세션을 찾을 수 없습니다."},
            status=404,
        )

    fields = _model_fields(LiveChatSession)
    now = timezone.now()

    # 2) 입력 값 정리
    session_type = (data.get("session_type") or "").strip()
    session_note = (data.get("session_note") or "").strip()
    session_detail = (data.get("session_detail") or "").strip()

    try:
        # 존재하는 필드에만 안전하게 채워 넣기
        if "session_type" in fields:
            setattr(obj, "session_type", session_type or None)
        if "session_note" in fields:
            setattr(obj, "session_note", session_note or None)
        if "session_detail" in fields:
            setattr(obj, "session_detail", session_detail or None)

        # memo 필드 있으면 요약/상세를 같이 넣어 주기 (비어 있을 때만)
        if "memo" in fields and (session_note or session_detail):
            if not getattr(obj, "memo", ""):
                obj.memo = session_note or session_detail

        if "processed_at" in fields:
            obj.processed_at = now
        if "ended_at" in fields and not getattr(obj, "ended_at", None):
            obj.ended_at = now

        # 3) 상태 전이
        mark_saved = getattr(obj, "mark_saved", None)
        if callable(mark_saved):
            # 모델에 헬퍼가 있으면 그걸 믿고 사용
            mark_saved()
        elif "status" in fields:
            # Status enum 이 있으면 쓰고, 아니면 문자열로
            Status = getattr(LiveChatSession, "Status", None)
            if Status is not None and hasattr(Status, "SAVED"):
                obj.status = Status.SAVED  # type: ignore[attr-defined]
            else:
                obj.status = "saved"

        obj.save()
    except Exception as e:
        log.exception("livechat api_livechat_save failed: %s", e)
        return JsonResponse(
            {"ok": False, "message": "상담 기록 저장 중 서버 오류가 발생했습니다."},
            status=500,
        )

    # 4) 마스터 콘솔에 'session_saved' 브로드캐스트 (실패해도 치명적 아님)
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

    # 5) 프런트로 응답
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
#  상담 종료 API (한 번만 종료 + 한 번만 메시지)
# ─────────────────────────────────────
def _get_session_by_sid_or_room(
    sid: Optional[int], room: str
) -> Tuple[Optional[LiveChatSession], Set[str]]:
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

    - 세션 상태를 "종료(저장 필요)" 상태로 바꾸고
    - ended_at 타임스탬프를 찍은 뒤
    - WebSocket(room)에 'end' 타입 메시지를 브로드캐스트해서
      고객/상담사 화면 모두에 종료 안내를 전달한다.
    """
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return JsonResponse({"ok": False, "message": "잘못된 JSON 입니다."}, status=400)

    sid = _to_int(payload.get("session_id"))
    room = (payload.get("room") or "").strip()
    end_text = (payload.get("text") or "").strip() or END_MESSAGE

    obj, fields = _get_session_by_sid_or_room(sid, room)
    if obj is None:
        return JsonResponse({"ok": False, "message": "세션을 찾을 수 없습니다."}, status=404)

    now = timezone.now()
    changed_fields: list[str] = []

    # 1) 상태/종료시간 갱신 (가능하면 모델 헬퍼 사용)
    try:
        mark_ended_need_save = getattr(obj, "mark_ended_need_save", None)
        if callable(mark_ended_need_save):
            # 모델 내부 로직에 맡김 (status / ended_at 세팅)
            mark_ended_need_save()
        else:
            # enum 기반 상태가 있으면 ENDED_NEED_SAVE, 아니면 문자열 "ended"
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

        # mark_ended_need_save 가 ended_at 을 보장하지 않을 수도 있으니 보정
        if "ended_at" in fields and not getattr(obj, "ended_at", None):
            obj.ended_at = now
            if "ended_at" not in changed_fields:
                changed_fields.append("ended_at")

        if changed_fields:
            obj.save(update_fields=changed_fields)
        else:
            obj.save()
    except Exception:
        log.exception("api_livechat_end: session status update failed")

    ts = int(now.timestamp() * 1000)
    room_name = getattr(obj, "room", room)

    # 2) 방에 종료 메시지 브로드캐스트 (고객/상담사 화면)
    try:
        _send_room(
            room_name,
            {
                "type": "end",
                "sender": "operator",
                "text": end_text,
                "ts": ts,
                "room": room_name,
                "session_id": getattr(obj, "id", None),
            },
        )
    except Exception:
        log.exception("api_livechat_end: room broadcast failed")

    # 3) 마스터 콘솔에도 'session_ended' 이벤트 브로드캐스트
    try:
        _send_master(
            {
                "type": "session_ended",
                "room": room_name,
                "session_id": getattr(obj, "id", None),
                "status": getattr(obj, "status", None),
                "ended_at": getattr(obj, "ended_at", None),
                "ts": ts,
            }
        )
    except Exception:
        log.exception("api_livechat_end: master broadcast failed")

    return JsonResponse(
        {
            "ok": True,
            "message": "상담을 종료했습니다.",
            "session_id": getattr(obj, "id", None),
            "room": room_name,
            "session_status": getattr(obj, "status", None),
        }
    )


# ─────────────────────────────────────
#  최근 세션/저장/정리 (운영자용)
# ─────────────────────────────────────
@staff_member_required
@require_GET
def api_livechat_recent_sessions(request: HttpRequest) -> HttpResponse:
    """
    /api/livechat/recent-sessions/ (또는 운영자용 URL에서 재사용)
    - JS가 HTML/JSON 둘 다 받을 수 있게 응답을 유연하게.
    """
    fields = _model_fields(LiveChatSession)
    qs = LiveChatSession.objects.all()

    # 기본 정렬: 최신
    if "created_at" in fields:
        qs = qs.order_by("-created_at")
    else:
        qs = qs.order_by("-id")

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
    - 운영자 후처리 저장(세션 메모/유형/상세)
    - 저장 후 master로 session_saved 브로드캐스트(최근세션 실시간 갱신 트리거)
    - ★ 여기서 session_note/session_detail 을 채워 넣으면
      api_livechat_next_session 에서 '기록 완료'로 인식함.
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

    # 입력
    stype = str(data.get("session_type") or "").strip()
    snote = str(data.get("session_note") or "").strip()
    sdetail = str(data.get("session_detail") or "").strip()

    try:
        if "session_type" in fields and stype:
            s.session_type = stype
        if "session_note" in fields and snote:
            s.session_note = snote
        if "session_detail" in fields and sdetail:
            s.session_detail = sdetail
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

    _send_master(
        {
            "type": "session_saved",
            "room": s.room,
            "session_id": s.id,
            "ts": int(now.timestamp() * 1000),
        }
    )

    return JsonResponse({"ok": True, "session_id": s.id})


@staff_member_required
@csrf_protect
@require_POST
def live_chat_cleanup_view(request: HttpRequest) -> JsonResponse:
    """
    /ragadmin/live-chat/cleanup/
    - {mode:"today"}: 오늘 생성된 세션 중 종료 안된 것들을 ended로 바꾸기
    - {session_id:123}: 단일 세션 삭제(최근세션 리스트에서 삭제 버튼)
    """
    data = _json_body(request)
    fields = _model_fields(LiveChatSession)
    now = timezone.now()

    # 1) 단일 삭제
    session_id = str(data.get("session_id") or "").strip()
    if session_id.isdigit():
        sid = int(session_id)
        try:
            LiveChatSession.objects.filter(id=sid).delete()
        except Exception as e:
            log.warning("cleanup delete failed: %s", e)
            return JsonResponse({"ok": False, "error": "delete_failed"}, status=200)

        _send_master(
            {
                "type": "session_deleted",
                "session_id": sid,
                "ts": int(now.timestamp() * 1000),
            }
        )
        return JsonResponse({"ok": True})

    # 2) 오늘 정리
    mode = str(data.get("mode") or "").strip()
    if mode == "today":
        qs = LiveChatSession.objects.all()
        today = timezone.localdate()
        if "created_at" in fields:
            qs = qs.filter(created_at__date=today)
        # status가 있으면 ended/done/종료 제외
        if "status" in fields:
            exclude_statuses = ["ended", "done", "종료"]
            try:
                Status = LiveChatSession.Status
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
def livechat_client_room_view(request: HttpRequest, room: str) -> HttpResponse:
    """
    /c/<room>/  상담 전용 페이지
    - LiveChatSession.room 기준으로 세션 하나 찾고 클라이언트 템플릿 렌더
    """
    session = LiveChatSession.objects.filter(room=room).order_by("-id").first()
    if not session:
        raise Http404("유효하지 않은 상담 세션입니다.")

    ctx = {
        "session": session,
        "room": getattr(session, "room", room),
        "room_token": getattr(session, "room", room),
        "session_id": getattr(session, "id", None),
        "SERVICE_NAME": getattr(settings, "SERVICE_NAME", "김동건 포트폴리오"),
        "LIVECHAT_END_URL": reverse("livechat:api_livechat_end"),
    }
    return render(request, "ragapp/livechat/client_room.html", ctx)


# ─────────────────────────────────────
#  채팅기록 확인
# ─────────────────────────────────────
@staff_member_required
@require_GET
def livechat_session_log_view(request: HttpRequest, session_id: int) -> HttpResponse:
    """
    /ragadmin/live-chat/session/<session_id>/
    - 상담 한 건 전체 채팅 로그를 한 페이지에서 보여주는 뷰
    """
    MessageModel = _get_message_model()
    if not MessageModel:
        raise Http404("메시지 모델이 활성화되어 있지 않습니다.")

    session = LiveChatSession.objects.filter(id=session_id).first()
    if not session:
        raise Http404("해당 상담 세션을 찾을 수 없습니다.")

    messages = MessageModel.objects.filter(session_id=session.id).order_by("created_at")

    ctx = {
        "session": session,
        "messages": messages,
        "SERVICE_NAME": getattr(settings, "SERVICE_NAME", "김동건 포트폴리오"),
    }
    return render(request, "ragadmin/live_chat_session_log.html", ctx)


# ─────────────────────────────────────
#  간단 가용성 체크 (예전용)
# ─────────────────────────────────────
@require_GET
def livechat_availability_api(request: HttpRequest) -> JsonResponse:
    """
    /api/livechat/availability/
    - 예전 프론트에서 쓰던 단순 가용성 플래그
    - 지금 구조에서는 항상 available=True 로 응답 (status API는 agent_api에서 별도 제공)
    """
    return JsonResponse({"ok": True, "available": True})


# ─────────────────────────────────────
# 레거시 이름 호환
# ─────────────────────────────────────
livechat_request_api = api_livechat_request
live_chat_recent_sessions = api_livechat_recent_sessions
livechat_recent_sessions_view = api_livechat_recent_sessions
livechat_next_session_api = api_livechat_next_session
