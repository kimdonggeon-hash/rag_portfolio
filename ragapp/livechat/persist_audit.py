# ragapp/livechat/persist_audit.py
from __future__ import annotations

import os
import logging
from typing import Any, Dict, Optional

try:
    from django.conf import settings
except Exception:  # pragma: no cover
    settings = None  # type: ignore


log = logging.getLogger("ragapp.livechat.persist")


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


def _debug_enabled() -> bool:
    v = None
    if settings is not None:
        v = getattr(settings, "LIVECHAT_PERSIST_DEBUG", None)
    if v is None:
        v = os.getenv("LIVECHAT_PERSIST_DEBUG")
    return _boolish(v, default=False)


def _safe_payload(**kw: Any) -> Dict[str, Any]:
    """
    절대 본문(text/content)을 넣지 말 것.
    """
    allow = {
        "event", "reason",
        "model", "table",
        "room", "sid",
        "sender", "type",
        "len", "ts",
        "fields",
        "exc_type", "exc_msg",
    }
    out: Dict[str, Any] = {}
    for k, v in kw.items():
        if k not in allow:
            continue
        if v is None:
            continue
        if k == "exc_msg":
            s = str(v)
            out[k] = s[:220]
        else:
            out[k] = v
    return out


def info(event: str, **kw: Any) -> None:
    if not _debug_enabled():
        return
    payload = _safe_payload(event=event, **kw)
    log.info("LIVECHAT_PERSIST %s", payload)


def warn(event: str, **kw: Any) -> None:
    payload = _safe_payload(event=event, **kw)
    log.warning("LIVECHAT_PERSIST %s", payload)


def error(event: str, exc: Optional[BaseException] = None, **kw: Any) -> None:
    if exc is not None:
        kw.setdefault("exc_type", type(exc).__name__)
        kw.setdefault("exc_msg", str(exc))
    payload = _safe_payload(event=event, **kw)
    # stacktrace는 남기되, 본문은 절대 넣지 않음(kw에 넣는 것 자체를 금지)
    log.exception("LIVECHAT_PERSIST %s", payload)
