# ragsite/asgi.py
from __future__ import annotations

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ragsite.settings")

from django.core.asgi import get_asgi_application
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter

# ✅ settings 초기화 이후에 import
from ragsite.routing import websocket_urlpatterns

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AuthMiddlewareStack(
            URLRouter(websocket_urlpatterns)
        ),
    }
)
