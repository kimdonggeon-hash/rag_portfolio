# ragapp/services/chat_log_service.py
from __future__ import annotations

import logging
from typing import Type, Optional, cast

from django.contrib.auth import get_user_model
from django.contrib.auth.base_user import AbstractBaseUser

from ragapp.models_chat_retention import LiveChatMessage, ChatEvidence

log = logging.getLogger(__name__)

UserModel: Type[AbstractBaseUser] = cast(Type[AbstractBaseUser], get_user_model())


def save_livechat_message(
    *,
    session,
    role: str,
    content: str,
    by_user: AbstractBaseUser | None = None,
) -> Optional[LiveChatMessage]:
    """
    ✅ session=None일 때 NOT NULL(session_id) 크래시 방지
    ✅ 가능하면 store.save_ws_message()로 통일(세션 보정/room 업데이트/abuse snapshot 흐름)
    """
    if session is None or getattr(session, "pk", None) is None:
        log.warning("save_livechat_message skipped: session is None/unsaved")
        return None

    room = str(getattr(session, "room", "") or "")
    sid = int(getattr(session, "pk"))

    # 1) store 경로(권장)
    try:
        from ragapp.livechat import store as st

        mid = st.save_ws_message(
            room=room,
            session_id=sid,
            sender_norm=str(role or "system"),
            effective_type="message",
            body=str(content or ""),
        )
        if mid:
            msg = LiveChatMessage.objects.filter(id=int(mid)).first()
        else:
            msg = None
    except Exception:
        msg = None

    # 2) store 실패 시 fallback (세션은 확실히 있으니 안전)
    if msg is None:
        msg = LiveChatMessage.objects.create(
            session=session,
            role=str(role or "system")[:16],  # role max_length=16 보호
            content=str(content or ""),
            flagged_by=None,  # 아래에서 필요하면 세팅
        )

    # (기존 동작 유지) user가 아닌 발화에 by_user 있으면 flagged_by 기록
    if by_user and str(role).strip().lower() != "user":
        try:
            msg.flagged_by = by_user
            msg.save(update_fields=["flagged_by"])
        except Exception:
            pass

    return msg


def promote_to_evidence(
    *,
    message: LiveChatMessage,
    reason: str = "manual",
    by_user: AbstractBaseUser | None = None,
) -> ChatEvidence:
    if message is None or getattr(message, "pk", None) is None:
        raise ValueError("promote_to_evidence: message is None/unsaved")

    return ChatEvidence.objects.create(
        session=message.session,
        message=message,
        captured_text=message.content,
        reason=reason,
        created_by=by_user,
    )
