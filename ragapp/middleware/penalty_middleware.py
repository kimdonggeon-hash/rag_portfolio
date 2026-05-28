# ragapp/middleware/penalty_middleware.py
from __future__ import annotations

import logging
import time
from threading import Lock

from django.http import JsonResponse, HttpResponse
from django.shortcuts import render

from ragapp.services.image_moderation import get_actor_key, is_actor_blocked

log = logging.getLogger(__name__)


# 간단 TTL 캐시: 같은 actor_key에 대해 짧은 시간 DB 조회를 반복하지 않게 함
# - blocked는 좀 더 길게(예: 30초)
# - not blocked는 짧게(예: 8초)  → 정책 반영 지연을 줄임
_CACHE: dict[str, tuple[bool, str, float]] = {}
_CACHE_LOCK = Lock()
_CACHE_TTL_OK = 8.0
_CACHE_TTL_BLOCKED = 30.0
_CACHE_MAX = 5000  # 인스턴스당 메모리 보호


def _cache_get(actor_key: str) -> tuple[bool, str] | None:
    now = time.time()
    with _CACHE_LOCK:
        it = _CACHE.get(actor_key)
        if not it:
            return None
        blocked, msg, exp = it
        if exp <= now:
            _CACHE.pop(actor_key, None)
            return None
        return blocked, msg


def _cache_set(actor_key: str, blocked: bool, msg: str) -> None:
    now = time.time()
    ttl = _CACHE_TTL_BLOCKED if blocked else _CACHE_TTL_OK
    exp = now + ttl
    with _CACHE_LOCK:
        # 너무 커지면 대충 비우기(간단)
        if len(_CACHE) > _CACHE_MAX:
            _CACHE.clear()
        _CACHE[actor_key] = (blocked, msg, exp)


class PenaltyMiddleware:
    """
    기간/영구 제한이면 전 기능 차단.
    (정적/법무/헬스/운영콘솔 등은 예외 허용)
    """
    ALLOW_PREFIXES = (
        "/static/",
        "/legal/",
        "/favicon.ico",
        "/robots.txt",
        "/sitemap.xml",
        "/healthz",
        "/admin/",
        "/ragadmin/",
        "/.well-known/",   # (선택) 인증서/검증용
        # "/uploads/",     # (선택) 차단 페이지에서 업로드 리소스가 필요하면 허용
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = getattr(request, "path_info", None) or request.path or "/"

        # 0) OPTIONS(프리플라이트) 같은 건 그냥 통과시키는 게 안전
        #    (여기서 막히면 브라우저가 본 요청을 아예 못 보냄)
        if request.method == "OPTIONS":
            return self.get_response(request)

        # 1) allowlist prefix는 무조건 통과
        if any(path.startswith(p) for p in self.ALLOW_PREFIXES):
            return self.get_response(request)

        # 2) 스태프는 차단 제외
        try:
            u = getattr(request, "user", None)
            if u and u.is_authenticated and u.is_staff:
                return self.get_response(request)
        except Exception:
            # user 접근 이슈가 있어도 일단 통과(순서 문제일 수 있음)
            return self.get_response(request)

        # 3) 차단 체크 (장애 시 서비스 전체가 죽지 않게 fail-open)
        try:
            actor_key = get_actor_key(request)

            cached = _cache_get(actor_key)
            if cached is not None:
                blocked, msg = cached
            else:
                blocked, msg = is_actor_blocked(actor_key)
                _cache_set(actor_key, blocked, msg)

        except Exception as e:
            log.exception("PenaltyMiddleware check failed: %s", e)
            return self.get_response(request)

        if not blocked:
            return self.get_response(request)

        # 4) API면 JSON, 페이지면 템플릿/텍스트
        accept = (request.headers.get("Accept") or "")
        is_xhr = (request.headers.get("X-Requested-With") == "XMLHttpRequest")
        is_api_path = path.startswith("/api/")  # ✅ API 경로 규칙 있으면 도움 됨

        if ("application/json" in accept) or is_xhr or is_api_path:
            return JsonResponse(
                {"ok": False, "error": "SUSPENDED", "message": msg},
                status=403,
                json_dumps_params={"ensure_ascii": False},
            )

        try:
            return render(request, "ragapp/suspended.html", {"message": msg}, status=403)
        except Exception:
            return HttpResponse(msg, status=403)
