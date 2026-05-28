from __future__ import annotations

import re
import time
from typing import Any, Dict, Tuple

from django.conf import settings
from django.core.cache import cache

_FP_RE = re.compile(r"^[0-9a-f]{12}$")


def ban_key(fp: str) -> str:
    return f"board:ban:{fp}"


def ban_score_key(fp: str) -> str:
    return f"board:ban_score:{fp}"


def ban_index_key() -> str:
    return "board:ban_index:v1"


def ban_threshold() -> int:
    try:
        return int(getattr(settings, "BOARD_BAN_THRESHOLD", 3))
    except Exception:
        return 3


def ban_ttl_sec() -> int:
    try:
        return int(getattr(settings, "BOARD_BAN_TTL_SEC", 86400))
    except Exception:
        return 86400


def ban_index_ttl_sec() -> int:
    try:
        return int(getattr(settings, "BOARD_BAN_INDEX_TTL_SEC", 7 * 86400))
    except Exception:
        return 7 * 86400


def _now() -> int:
    return int(time.time())


def _valid_fp(fp: str) -> bool:
    return bool(_FP_RE.match((fp or "").strip().lower()))


def ban_seconds_left(ban_info: Any) -> int | None:
    try:
        if isinstance(ban_info, dict) and "until" in ban_info:
            return max(0, int(ban_info["until"]) - _now())
    except Exception:
        pass
    return None


def ban_index_upsert(fp: str, until: int) -> None:
    fp = (fp or "").strip().lower()
    if not _valid_fp(fp):
        return

    key = ban_index_key()
    ttl = ban_index_ttl_sec()
    now = _now()

    try:
        lst = cache.get(key) or []
        if not isinstance(lst, list):
            lst = []
    except Exception:
        lst = []

    found = False
    for it in lst:
        if isinstance(it, dict) and it.get("fp") == fp:
            it["until"] = int(until)
            it["updated"] = now
            found = True
            break

    if not found:
        lst.insert(0, {"fp": fp, "until": int(until), "updated": now})

    if len(lst) > 500:
        lst = lst[:500]

    try:
        cache.set(key, lst, ttl)
    except Exception:
        pass


def set_ban(fp: str, seconds: int, meta: Dict[str, Any] | None = None) -> Tuple[bool, int]:
    """
    return (ok, until)
    """
    fp = (fp or "").strip().lower()
    if not _valid_fp(fp):
        return (False, 0)

    seconds = int(seconds)
    if seconds < 60:
        seconds = 60
    if seconds > 30 * 86400:
        seconds = 30 * 86400

    until = _now() + seconds
    payload: Dict[str, Any] = {"until": until}
    if meta:
        payload.update(meta)

    try:
        cache.set(ban_key(fp), payload, seconds)
    except Exception:
        return (False, 0)

    ban_index_upsert(fp, until)
    return (True, until)


def clear_ban(fp: str) -> None:
    fp = (fp or "").strip().lower()
    if not _valid_fp(fp):
        return
    try:
        cache.delete(ban_key(fp))
        cache.delete(ban_score_key(fp))
    except Exception:
        pass


def add_points(fp: str, points: int, *, ttl: int | None = None) -> Dict[str, Any]:
    """
    fp에 ban 점수 누적 → 임계치 도달 시 자동 ban(기본 24h)
    return dict:
      {
        ok: bool,
        fp: str,
        score: int,
        threshold: int,
        banned: bool,
        until: int|0,
      }
    """
    fp = (fp or "").strip().lower()
    if not _valid_fp(fp):
        return {"ok": False, "fp": fp, "score": 0, "threshold": ban_threshold(), "banned": False, "until": 0}

    threshold = ban_threshold()
    ttl = int(ttl or ban_ttl_sec())

    # 이미 ban이면 그대로 반환
    existing = cache.get(ban_key(fp))
    if existing:
        left = ban_seconds_left(existing)
        return {"ok": True, "fp": fp, "score": threshold, "threshold": threshold, "banned": True, "until": int(existing.get("until", 0) if isinstance(existing, dict) else 0)}

    key = ban_score_key(fp)
    try:
        cur = cache.get(key)
        if cur is None:
            newv = int(points)
            cache.set(key, newv, ttl)
        else:
            try:
                newv = int(cache.incr(key, int(points)))  # type: ignore
            except Exception:
                newv = int(cur) + int(points)
                cache.set(key, newv, ttl)
    except Exception:
        newv = int(points)

    if int(newv) >= threshold:
        ok, until = set_ban(fp, ttl, meta={"score": int(newv), "reason": "auto"})
        try:
            cache.delete(key)
        except Exception:
            pass
        return {"ok": ok, "fp": fp, "score": int(newv), "threshold": threshold, "banned": True, "until": until}

    return {"ok": True, "fp": fp, "score": int(newv), "threshold": threshold, "banned": False, "until": 0}
