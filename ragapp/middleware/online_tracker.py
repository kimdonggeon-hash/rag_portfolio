# ragapp/middleware/online_tracker.py
from __future__ import annotations

import time
import hashlib
from typing import Any, Set

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.deprecation import MiddlewareMixin


class OnlineTrackerMiddleware(MiddlewareMixin):
    """
    - 최근 N초(RUNTIME_ONLINE_TTL_SEC) 안에 요청한 사용자를 "온라인"으로 본다.
    - 온라인 수가 RUNTIME_ONLINE_HARD_LIMIT를 넘으면
      → 일반 사용자는 503 페이지, staff/superuser는 통과.
    - /admin, /api/usage/status/, /healthz, /robots.txt, 정적/업로드는 차단/집계에서 일부 제외.
    """

    MAP_KEY = "runtime:online:keys"
    SEEN_PREFIX = "runtime:online:seen:"

    def __init__(self, get_response):
        super().__init__(get_response)
        self.ttl: int = getattr(settings, "RUNTIME_ONLINE_TTL_SEC", 60)
        self.limit: int = getattr(settings, "RUNTIME_ONLINE_HARD_LIMIT", 0)

    # Django 3.2 호환을 위해 process_request만 사용
    def process_request(self, request: HttpRequest) -> HttpResponse | None:
        path = (request.path or "/").strip() or "/"

        # 1) 정적/업로드/파비콘은 집계 X
        static_url = getattr(settings, "STATIC_URL", "/static/")
        media_url = getattr(settings, "MEDIA_URL", "/uploads/")

        if path.startswith(static_url) or path.startswith(media_url):
            return None
        if path.startswith("/favicon"):
            return None

        # 2) 접속자 fingerprint 만들기
        fp = self._fingerprint(request)
        now = time.time()

        # 3) 캐시에 이 fingerprint를 "살아있다"고 표시
        active: Set[str] = cache.get(self.MAP_KEY) or set()
        if not isinstance(active, set):
            active = set()

        active.add(fp)
        cache.set(self.MAP_KEY, active, self.ttl + 10)
        cache.set(f"{self.SEEN_PREFIX}{fp}", now, self.ttl + 10)

        # 4) 만료된 애들 정리하면서 현재 온라인 수 계산
        online_count = self._count_active(now)

        # 뷰/템플릿에서 쓸 수 있게 request에 심어두기
        setattr(request, "online_count", online_count)
        cache.set("runtime:online:count", online_count, self.ttl)

        # 5) 하드 제한 넘었는지 확인
        limit = int(self.limit or 0)
        user = getattr(request, "user", None)
        is_staff = bool(
            getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)
        )

        # 관리/헬스/상태 API는 차단하지 않음
        if path.startswith("/admin/") or path.startswith("/api/usage/status/") \
           or path.startswith("/healthz") or path == "/robots.txt":
            return None

        # 실제 차단 조건: 하드 제한이 있고, 그걸 넘었고, staff가 아닐 때
        if limit > 0 and online_count > limit and not is_staff:
            # 너무 많은 이용자 → 503 페이지 반환
            try:
                resp = render(
                    request,
                    "ragapp/too_many_users.html",
                    {
                        "online_count": online_count,
                        "limit": limit,
                    },
                    status=503,
                )
            except Exception:
                # 템플릿 없으면 최소한의 HTML이라도
                html = (
                    "<!doctype html><html><head><meta charset='utf-8'>"
                    "<title>잠시 후 다시 접속해 주세요</title></head>"
                    "<body><h1>잠시만 이용이 어려워요.</h1>"
                    f"<p>현재 접속 인원이 많아 일시적으로 제한 중입니다. "
                    f"(현재 {online_count}명 / 최대 {limit}명)</p>"
                    "<p>잠시 후 다시 접속해 주세요.</p>"
                    "</body></html>"
                )
                resp = HttpResponse(html, status=503)

            # 브라우저/클라이언트에게 "대략 60초 뒤에 다시 시도해라" 힌트
            resp["Retry-After"] = "60"
            return resp

        return None

    # ───────────────── 내부 유틸 ─────────────────

    def _count_active(self, now: float) -> int:
        active: Set[str] = cache.get(self.MAP_KEY) or set()
        if not isinstance(active, set):
            active = set()

        alive: Set[str] = set()
        for fp in list(active):
            ts = cache.get(f"{self.SEEN_PREFIX}{fp}")
            if ts and now - ts <= self.ttl:
                alive.add(fp)
            else:
                # 만료된 fingerprint는 캐시에서 제거
                cache.delete(f"{self.SEEN_PREFIX}{fp}")

        cache.set(self.MAP_KEY, alive, self.ttl + 10)
        return len(alive)

    def _fingerprint(self, request: HttpRequest) -> str:
        """
        - 로그인 유저: user_id 기준
        - 비로그인: IP + User-Agent 기준 (해시)
        """
        user = getattr(request, "user", None)
        if getattr(user, "is_authenticated", False):
            base = f"user:{user.pk}"
        else:
            ip = (
                (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
                or request.META.get("REMOTE_ADDR", "")
            )
            ua = request.META.get("HTTP_USER_AGENT", "")
            base = f"anon:{ip}:{ua}"

        h = hashlib.sha256(base.encode("utf-8")).hexdigest()
        # 너무 길 필요는 없으니 앞 32글자만
        return h[:32]


def get_online_count() -> int:
    """
    /api/usage/status/ 같은 곳에서 쓸 수 있는 헬퍼.
    """
    value: Any = cache.get("runtime:online:count")
    try:
        return int(value) if value is not None else 0
    except Exception:
        return 0
