# ragapp/livechat/agent_api.py
from __future__ import annotations

import logging
from typing import Dict

from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET

log = logging.getLogger(__name__)

# ─────────────────────────────────────
# 상담사 온라인 카운터 (프로세스 단위)
# ─────────────────────────────────────
_OPERATOR_COUNT: int = 0


def _normalize() -> int:
    """
    내부 카운터가 0 미만으로 내려가는 걸 방지.
    """
    global _OPERATOR_COUNT
    if _OPERATOR_COUNT < 0:
        _OPERATOR_COUNT = 0
    return _OPERATOR_COUNT


def mark_operator_online() -> None:
    """
    MasterConsumer.connect() 에서 호출.
    상담사 콘솔 WebSocket 연결 1개당 +1.
    """
    global _OPERATOR_COUNT
    try:
        _OPERATOR_COUNT += 1
        _normalize()
        log.debug("livechat operator ++ -> %s", _OPERATOR_COUNT)
    except Exception as e:
        log.warning("mark_operator_online failed: %s", e)


def mark_operator_offline() -> None:
    """
    MasterConsumer.disconnect() 에서 호출.
    WebSocket 끊길 때마다 -1.
    """
    global _OPERATOR_COUNT
    try:
        _OPERATOR_COUNT -= 1
        _normalize()
        log.debug("livechat operator -- -> %s", _OPERATOR_COUNT)
    except Exception as e:
        log.warning("mark_operator_offline failed: %s", e)


def get_status() -> Dict[str, object]:
    """
    내부 헬퍼: 현재 온라인 상담사 수와 available 플래그를 계산.
    """
    count = _normalize()
    return {
        "online_count": count,
        "available": bool(count > 0),
    }


# ─────────────────────────────────────
# QARAG / 프론트에서 호출하는 상태 API
# ─────────────────────────────────────
@require_GET
def livechat_status_api(request: HttpRequest) -> JsonResponse:
    """
    /api/livechat/status/

    응답 예:
    {
      "ok": true,
      "available": true,   # 상담사 1명 이상이면 true
      "online_count": 1
    }
    """
    status = get_status()
    return JsonResponse(
        {
            "ok": True,
            "available": status["available"],
            "online_count": status["online_count"],
        }
    )
