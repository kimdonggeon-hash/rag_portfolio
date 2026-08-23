from __future__ import annotations

import hashlib
from django.conf import settings
from django.core.cache import cache

from ragapp.services.usage_limiter import get_cookie_cid


def _get_ip(request) -> str:
    try:
        xff = request.META.get("HTTP_X_FORWARDED_FOR")
        if xff:
            return xff.split(",")[0].strip()
    except Exception:
        pass
    return request.META.get("REMOTE_ADDR") or "0.0.0.0"


def client_fingerprint(request) -> str:
    """✅ 쿠키(dg_cid) 우선 — 브라우저를 바꿔도(사파리↔크롬) 같은 기기면
    같은 fingerprint가 나오도록 한다(레이트리밋/중복제출 우회 방지)."""
    secret = getattr(settings, "LOG_IP_HASH_SECRET", "") or "board-rl-secret"
    cid = get_cookie_cid(request)
    if cid:
        raw = f"{secret}|cid:{cid}"
    else:
        raw = f"{secret}|ip:{_get_ip(request)}"
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
