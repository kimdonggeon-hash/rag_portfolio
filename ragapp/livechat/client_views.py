# ragapp/livechat/client_views.py
from __future__ import annotations

import json
from typing import Any, Dict

from django.http import HttpRequest, HttpResponse, Http404
from django.shortcuts import render
from django.utils.safestring import mark_safe

from ragapp.models import LiveChatSession


def client_page_view(request: HttpRequest, room: str) -> HttpResponse:
    """
    /c/<room>/
    - 사용자용 상담 전용 페이지
    - room 값으로 LiveChatSession을 찾아서 존재하면 입장 허용
    - JS에서 사용할 설정(__LIVECHAT_CONFIG__)만 내려준다.
    """
    room = (room or "").strip()
    if not room:
        raise Http404("room not specified")

    session = (
        LiveChatSession.objects.filter(room=room)
        .order_by("-id")
        .first()
    )
    if not session:
        raise Http404("상담 세션을 찾을 수 없습니다.")

    config: Dict[str, Any] = {
        "mode": "client_page",
        "roomToken": room,
        "sessionId": session.id,
    }

    ctx = {
        "session": session,
        "livechat_config_json": mark_safe(json.dumps(config, ensure_ascii=False)),
    }
    return render(request, "ragapp/livechat/client_page.html", ctx)
