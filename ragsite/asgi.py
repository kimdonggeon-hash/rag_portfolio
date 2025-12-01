# ragsite/asgi.py
from __future__ import annotations

import os

from django.core.asgi import get_asgi_application

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter

# 🔹 방금 만든 websocket_urlpatterns import
from ragsite.routing import websocket_urlpatterns

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ragsite.settings")

django_asgi_app = get_asgi_application()

application = ProtocolTypeRouter(
    {
        # HTTP 요청은 기존 Django ASGI로 처리
        "http": django_asgi_app,
        # WebSocket은 Channels + URLRouter로 처리
        "websocket": AuthMiddlewareStack(
            URLRouter(websocket_urlpatterns)
        ),
    }
)
