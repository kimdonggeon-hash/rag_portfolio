# ragapp/middleware/presence.py
from __future__ import annotations

import time
from typing import Dict, Iterable

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest


try:
    # 네가 이미 쓰는 키 생성기( IP/UA 해시 기반 ) 재사용
    from ragapp.services.usage_limiter import build_client_key  # type: ignore
except Exception:
    build_client_key = None  # type: ignore


PRESENCE_KEY = getattr(settings, "OPS_PRESENCE_CACHE_KEY", "ops:presence:v1")
PRESENCE_TTL_SEC = int(getattr(settings, "OPS_PRESENCE_TTL_SEC", 90))

EXCLUDE_PREFIXES: Iterable[str] = tuple(
    getattr(
        settings,
        "OPS_PRESENCE_EXCLUDE_PREFIXES",
        [
            "/static/",
            "/uploads/",
            "/media/",
            "/favicon.ico",
            "/robots.txt",
            "/healthz",
            "/api/ping",
        ],
    )
)


def _should_skip(path: str) -> bool:
    for p in EXCLUDE_PREFIXES:
        if path.startswith(p):
            return True
    return False


def _client_id(request: HttpRequest) -> str:
    # 가장 안정적인 키는 네 기존 build_client_key(해시) 재사용
    if callable(build_client_key):
        try:
            return str(build_client_key(request) or "")
        except Exception:
            pass

    # 폴백: 세션키/REMOTE_ADDR/UA 조합(대충)
    sk = getattr(getattr(request, "session", None), "session_key", "") or ""
    ip = request.META.get("REMOTE_ADDR") or ""
    ua = request.META.get("HTTP_USER_AGENT") or ""
    raw = (sk + "|" + ip + "|" + ua).strip()
    return raw[:200]


class PresenceMiddleware:
    """
    ✅ '최근 N초 내에 요청한 유저 수'를 근사로 집계
    - 캐시(LocMem/Redis)에 dict로 저장: {client_id: last_seen_epoch}
    - Cloud Run에서 '인스턴스가 여러 개'면
      *공유 캐시(예: Redis/Memorystore)*가 아니면 인스턴스별로 흔들릴 수 있음.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.key = PRESENCE_KEY
        self.ttl = int(PRESENCE_TTL_SEC)

    def __call__(self, request: HttpRequest):
        path = request.path or "/"

        # 일단 response 먼저(실패/차단이어도 기록하고 싶으면 아래 순서를 바꿔도 됨)
        resp = self.get_response(request)

        try:
            if _should_skip(path):
                return resp

            cid = _client_id(request)
            if not cid:
                return resp

            now = int(time.time())
            cutoff = now - self.ttl

            m = cache.get(self.key) or {}
            if not isinstance(m, dict):
                m = {}

            # 업데이트
            m[str(cid)] = now

            # prune (오래된 것 제거)
            # 규모가 커질 수 있으니 상한을 둠
            if len(m) > 4000:
                # 오래된 것 먼저 날리는 방식
                for k, ts in list(m.items()):
                    try:
                        t = int(ts)
                    except Exception:
                        m.pop(k, None)
                        continue
                    if t < cutoff:
                        m.pop(k, None)

                # 그래도 크면 그냥 더 자름(최후 방어)
                if len(m) > 4500:
                    # 임의로 일부 제거
                    for i, k in enumerate(list(m.keys())):
                        if i >= 500:
                            break
                        m.pop(k, None)
            else:
                for k, ts in list(m.items()):
                    try:
                        t = int(ts)
                    except Exception:
                        m.pop(k, None)
                        continue
                    if t < cutoff:
                        m.pop(k, None)

            # TTL보다 조금 길게 유지
            cache.set(self.key, m, timeout=max(60, self.ttl * 3))
        except Exception:
            pass

        return resp
