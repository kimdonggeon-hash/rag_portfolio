# ragapp/livechat/ws_consumers.py
from __future__ import annotations

"""
⚠️ 레거시 호환용 모듈

예전 코드에서
    from ragapp.livechat.ws_consumers import MasterConsumer, RoomConsumer, UserRoomConsumer
같이 임포트해도,

항상 최신 구현이 들어있는
    ragapp.livechat.consumers.MasterConsumer / RoomConsumer
를 그대로 쓰도록 alias만 제공한다.
"""

from ragapp.livechat.consumers import MasterConsumer, RoomConsumer

# 예전 이름 호환
UserRoomConsumer = RoomConsumer