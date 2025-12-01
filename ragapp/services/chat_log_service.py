# ragapp/services/chat_log_service.py
from __future__ import annotations

from typing import Type, cast
from django.contrib.auth import get_user_model
from django.contrib.auth.base_user import AbstractBaseUser

from ragapp.models_chat_retention import LiveChatMessage, ChatEvidence

# 런타임에서 실제 User 모델 클래스
UserModel: Type[AbstractBaseUser] = cast(Type[AbstractBaseUser], get_user_model())


def save_livechat_message(
    *,
    session,
    role: str,
    content: str,
    by_user: AbstractBaseUser | None = None,
) -> LiveChatMessage:
    # save() 안에서 자동 분류 / purge_at 계산
    return LiveChatMessage.objects.create(
        session=session,
        role=role,
        content=content,
        flagged_by=(by_user if by_user and role != "user" else None),
    )


def promote_to_evidence(
    *,
    message: LiveChatMessage,
    reason: str = "manual",
    by_user: AbstractBaseUser | None = None,
) -> ChatEvidence:
    return ChatEvidence.objects.create(
        session=message.session,
        message=message,
        captured_text=message.content,
        reason=reason,
        created_by=by_user,
    )
