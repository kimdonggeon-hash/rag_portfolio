# ragapp/livechat/persist.py
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Set, Tuple

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ragapp.livechat.persist_safe_log import safe_log

log = logging.getLogger(__name__)


def _model_fields(model) -> Set[str]:
    try:
        return {f.name for f in model._meta.get_fields() if hasattr(f, "attname")}
    except Exception:
        return set()


def get_room_model():
    # 우선 direct import (가장 안정적)
    try:
        from ragapp.models import LiveChatRoom as R  # type: ignore
        return R
    except Exception:
        pass
    # 최후 fallback
    try:
        return apps.get_model("ragapp", "LiveChatRoom")
    except Exception:
        return None


def get_session_model():
    try:
        from ragapp.models import LiveChatSession as S  # type: ignore
        return S
    except Exception:
        pass
    try:
        return apps.get_model("ragapp", "LiveChatSession")
    except Exception:
        return None


def get_message_model():
    """
    단일 출처 우선순위:
    1) models_chat_retention.LiveChatMessage
    2) ragapp.models.LiveChatMessage
    3) apps.get_model("ragapp","LiveChatMessage")
    """
    try:
        from ragapp.models_chat_retention import LiveChatMessage as M  # type: ignore
        return M
    except Exception:
        pass
    try:
        from ragapp.models import LiveChatMessage as M  # type: ignore
        return M
    except Exception:
        pass
    try:
        return apps.get_model("ragapp", "LiveChatMessage")
    except Exception:
        return None


def _ensure_room_and_session(room: str) -> Tuple[Optional[Any], Optional[Any]]:
    """
    room 문자열만으로 최소 Room/Session을 보장.
    - 모델/필드 제약이 있으면 safe_log에 why 찍고 None 반환.
    """
    if not room:
        return None, None

    R = get_room_model()
    S = get_session_model()

    if R is None:
        safe_log("save_skip_no_model", level="warning", model="LiveChatRoom", room=room)
        return None, None
    if S is None:
        safe_log("save_skip_no_model", level="warning", model="LiveChatSession", room=room)
        return None, None

    rf = _model_fields(R)
    sf = _model_fields(S)

    with transaction.atomic():
        # Room
        try:
            defaults: Dict[str, Any] = {}
            if "status" in rf:
                defaults["status"] = "open"
            # client_label/operator_id/last_question 등은 여기서 억지로 넣지 않음 (없어도 생성되게)
            room_obj, created = R.objects.get_or_create(room_id=room, defaults=defaults)
            if created:
                safe_log("room_created", room=room, room_pk=getattr(room_obj, "pk", None))
        except Exception as e:
            safe_log("room_create_error", level="error", room=room, why=repr(e))
            return None, None

        # Session (없으면 새로 1개)
        try:
            sess = S.objects.filter(room=room).order_by("-id").first()
            if sess is None:
                create_kwargs: Dict[str, Any] = {}
                if "room" in sf:
                    create_kwargs["room"] = room
                sess = S.objects.create(**create_kwargs)
                safe_log("session_created", room=room, session_id=getattr(sess, "id", None))
        except Exception as e:
            safe_log("session_create_error", level="error", room=room, why=repr(e))
            return room_obj, None

    return room_obj, sess


def persist_message(*, room: str | None, session_id: int | None, role: str, content: str) -> Optional[int]:
    """
    DB에 LiveChatMessage 1건 저장.
    - LIVECHAT_PERSIST_MESSAGES가 False면 저장하지 않음.
    - session_id 우선, 없으면 room 기준으로 LiveChatSession을 찾거나 없으면 생성.
    - 저장 성공 시 message pk 리턴.
    """
    if not getattr(settings, "LIVECHAT_PERSIST_MESSAGES", True):
        return None

    M = get_message_model()
    if M is None:
        safe_log("save_skip_no_model", level="warning", model="LiveChatMessage", room=room, session_id=session_id)
        return None

    text = (content or "").strip()
    if not text:
        return None

    S = get_session_model()
    if S is None:
        safe_log("save_skip_no_model", level="warning", model="LiveChatSession", room=room, session_id=session_id)
        return None

    s = None
    if session_id:
        s = S.objects.filter(id=session_id).first()

    if s is None and room:
        s = S.objects.filter(room=room).order_by("-id").first()

    # ✅ 핵심: 세션이 없으면 여기서 Room+Session을 강제로 만들어서 메시지 저장을 보장
    if s is None and room:
        _, s = _ensure_room_and_session(room)

    if s is None:
        safe_log("msg_skip_no_session", level="warning", room=room, session_id=session_id, text_len=len(text))
        return None

    mf = _model_fields(M)
    kwargs: Dict[str, Any] = {}

    # session FK
    if "session" in mf:
        kwargs["session"] = s
    elif "session_id" in mf:
        kwargs["session_id"] = getattr(s, "id", None)
    else:
        safe_log("msg_skip_no_session_field", level="warning", room=getattr(s, "room", None) or room, session_id=getattr(s, "id", None))
        return None

    # role/sender
    if "role" in mf:
        kwargs["role"] = role
    elif "sender" in mf:
        kwargs["sender"] = role

    # content/text/body/message
    if "content" in mf:
        kwargs["content"] = text
    elif "text" in mf:
        kwargs["text"] = text
    elif "body" in mf:
        kwargs["body"] = text
    elif "message" in mf:
        kwargs["message"] = text
    else:
        safe_log("msg_skip_no_content_field", level="warning", room=getattr(s, "room", None) or room, session_id=getattr(s, "id", None))
        return None

    # created_at 자동이면 OK. ts(ms) 필드 있으면 채워주기
    if "ts" in mf:
        kwargs["ts"] = int(timezone.now().timestamp() * 1000)

    obj = M.objects.create(**kwargs)
    pk = getattr(obj, "pk", None)
    safe_log("msg_saved", room=getattr(s, "room", None) or room, session_id=getattr(s, "id", None), msg_id=pk, text_len=len(text))
    return pk

def ensure_room_and_session(room_id: str):
    try:
        from ragapp.models import LiveChatRoom, LiveChatSession
    except Exception as e:
        safe_log("save_skip_no_model", level="warning", model="LiveChatRoom/Session", room=room_id, why=repr(e))
        return None, None

    now = timezone.now()

    try:
        with transaction.atomic():
            room_obj, created = LiveChatRoom.objects.get_or_create(
                room_id=room_id,
                defaults={
                    "client_label": "",
                    "last_question": "",
                    "status": "open",
                    # ✅ auto_now*가 아닐 수도 있으니 안전하게 직접 세팅
                    "created_at": now,
                    "updated_at": now,
                },
            )
            if not created:
                # ✅ 접속 시각 갱신(업데이트 컬럼이 NOT NULL이라 안전)
                LiveChatRoom.objects.filter(pk=room_obj.pk).update(updated_at=now)

            sess = LiveChatSession.objects.filter(room=room_id).order_by("-id").first()
            if sess is None:
                sess = LiveChatSession.objects.create(room=room_id)

            return room_obj, sess

    except Exception as e:
        safe_log("room_create_error", level="error", room=room_id, why=repr(e))
        return None, None