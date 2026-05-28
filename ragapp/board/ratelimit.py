from __future__ import annotations

import hashlib
from django.core.cache import cache


def _get_ip(request) -> str:
    try:
        xff = request.META.get("HTTP_X_FORWARDED_FOR")
        if xff:
            return xff.split(",")[0].strip()
    except Exception:
        pass
    return request.META.get("REMOTE_ADDR") or "0.0.0.0"


def client_fingerprint(request) -> str:
    ip = _get_ip(request)
    ua = (request.META.get("HTTP_USER_AGENT") or "-")[:120]
    raw = f"{ip}|{ua}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def hit(key: str, limit: int, window_sec: int) -> tuple[bool, int]:
    """
    True면 차단(한도 초과).
    """
    k = f"board:rl:{key}"
    v = cache.get(k)
    if v is None:
        cache.set(k, 1, window_sec)
        return (False, 1)

    try:
        n = int(v)
    except Exception:
        n = 0

    if n >= limit:
        return (True, n)

    try:
        n2 = cache.incr(k)
    except Exception:
        n2 = n + 1
        cache.set(k, n2, window_sec)
    return (False, int(n2))


def replay_guard(key: str, fingerprint: str, ttl_sec: int = 300) -> bool:
    """
    같은 fingerprint가 ttl 내 재요청이면 True(차단 권장).
    """
    k = f"board:replay:{key}:{fingerprint}"
    if cache.get(k):
        return True
    cache.set(k, 1, ttl_sec)
    return False
