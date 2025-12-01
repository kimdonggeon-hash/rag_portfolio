# ragsite/routing.py
from django.urls import re_path
from ragapp.livechat import consumers as livechat_consumers

# Channels 에서 scope["path"] 는 맨 앞 슬래시가 빠진 상태로 들어온다.
#  - 예) 클라이언트:  ws://host/ws/chat/master
#    → scope["path"] = "ws/chat/master"
# 그래서 정규식 앞에는 슬래시 안 붙이고, 끝 슬래시는 있어도/없어도 되게 "/?$" 처리.

websocket_urlpatterns = [
    # 운영자 로비(마스터) 채널: /ws/chat/master 또는 /ws/chat/master/
    re_path(r"^ws/chat/master/?$", livechat_consumers.MasterConsumer.as_asgi()),

    # 개별 상담 방: /ws/chat/<room> 또는 /ws/chat/<room>/
    re_path(r"^ws/chat/(?P<room>[^/]+)/?$", livechat_consumers.RoomConsumer.as_asgi()),
]
