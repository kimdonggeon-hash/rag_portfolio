# ragapp/services/chat_log.py 

from ragapp.models import ChatQueryLog

def log_chat_message(
    *,
    session_id: str,
    channel: str,
    role: str,
    message_type: str,
    content: str,
    answer_sources: dict | list | None = None,
    extra: dict | None = None,
) -> ChatQueryLog:
    return ChatQueryLog.objects.create(
        session_id=session_id,
        channel=channel,
        role=role,
        message_type=message_type,
        content=content,
        answer_sources=answer_sources or None,
        extra=extra or None,
    )
