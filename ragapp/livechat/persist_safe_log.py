# ragapp/livechat/persist_safe_log.py
from __future__ import annotations

import json
import os
import logging
from typing import Any, Dict

log = logging.getLogger(__name__)

_PREFIX = "LC_PERSIST"


def _boolish(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "t", "yes", "y", "on"):
        return True
    if s in ("0", "false", "f", "no", "n", "off"):
        return False
    return default


def _clip(v: Any, max_len: int = 160) -> Any:
    if v is None:
        return None
    if isinstance(v, (int, float, bool)):
        return v
    s = str(v)
    if len(s) > max_len:
        return s[:max_len] + "…"
    return s


def debug_enabled() -> bool:
    return _boolish(os.getenv("LIVECHAT_PERSIST_DEBUG"), default=False)


def stack_enabled() -> bool:
    # 기본은 False. 필요 시 Cloud Run env로 LIVECHAT_PERSIST_LOG_STACK=1 켜서 잠깐만.
    return _boolish(os.getenv("LIVECHAT_PERSIST_LOG_STACK"), default=False)


def safe_log(event: str, level: str = "info", **kv: Any) -> None:
    """
    ⚠️ 본문/PII를 절대 넣지 말 것.
    room/session_id/len 같은 메타만 기록.
    """
    payload: Dict[str, Any] = {"ev": event}

    # ✅ Cloud Run에서 “어느 서비스/리비전에서 찍힌 로그인지” 즉시 보이게
    payload["_svc"] = _clip(os.getenv("K_SERVICE"), 80)
    payload["_rev"] = _clip(os.getenv("K_REVISION"), 120)
    payload["_cfg"] = _clip(os.getenv("K_CONFIGURATION"), 80)
    payload["_host"] = _clip(os.getenv("HOSTNAME"), 80)

    for k, v in kv.items():
        payload[k] = _clip(v)

    msg = f"{_PREFIX} " + json.dumps(payload, ensure_ascii=False, sort_keys=True)

    if level == "warning":
        log.warning(msg, exc_info=stack_enabled())
    elif level == "error":
        log.error(msg, exc_info=stack_enabled())
    else:
        log.info(msg)
