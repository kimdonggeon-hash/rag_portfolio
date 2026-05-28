# ragapp/middleware/image_throttle_middleware.py
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.deprecation import MiddlewareMixin


def _client_ip(request: HttpRequest) -> str:
    # Cloud Run / 프록시 환경 고려 (X-Forwarded-For)
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        # "client, proxy1, proxy2" -> client
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or ""


def _ua(request: HttpRequest) -> str:
    return (request.META.get("HTTP_USER_AGENT", "") or "")[:220]


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "ignore")).hexdigest()[:16]


def _get_actor_key(request: HttpRequest) -> str:
    """
    네 프로젝트에 get_actor_key가 있으면 그걸 우선 사용.
    없으면 ip+ua 기반으로 최소한의 actor 키 생성.
    """
    try:
        from ragapp.services.image_moderation import get_actor_key  # type: ignore
        k = get_actor_key(request)
        if k:
            return str(k)
    except Exception:
        pass

    ip = _client_ip(request)
    ua = _ua(request)
    base = f"{ip}|{ua}"
    return "anon_" + _hash(base)


@dataclass(frozen=True)
class _Rule:
    method: str
    path_prefix: str
    cooldown_sec: int
    require_query_key: Optional[str] = None  # e.g. "q" (있을 때만 적용)


class ImageThrottleMiddleware(MiddlewareMixin):
    """
    이미지 기능만 서버에서 '마지막 안전망'으로 연타 방지.

    ✅ 제외
      - /admin/, /ragadmin/ (운영/어드민)
      - livechat 계열
      - staff 요청
      - 정적/헬스/법무/업로드 프록시

    ✅ 대상(필요한 것만)
      - POST /api/media/upsert         (이미지 업로드 접수/업서트)
      - POST /api/media/upsert_tags    (태그/캡션 업서트)
      - GET  /media/search/ (q 있을 때만)  (글로 이미지 찾기 실행)
      - GET  /api/media/keyword_search (q 있을 때만) (키워드 검색 API 쓰는 경우)
    """

    # 빠른 제외(경로 prefix)
    EXCLUDE_PREFIXES = (
        "/static/",
        "/healthz",
        "/legal/",
        "/favicon.ico",
        "/uploads/",
        "/admin/",
        "/ragadmin/",
        "/livechat/",
        "/api/live_chat/",
        "/api/qarag/",
        "/board/",
    )

    # 정확히 이것만 막자 (너의 urls.py 기준)
    RULES = (
        _Rule("POST", "/api/media/upsert", cooldown_sec=5),
        _Rule("POST", "/api/media/upsert_tags", cooldown_sec=3),
        _Rule("GET", "/media/search/", cooldown_sec=1, require_query_key="q"),
        _Rule("GET", "/api/media/keyword_search", cooldown_sec=1, require_query_key="q"),
    )

    def process_request(self, request: HttpRequest) -> Optional[HttpResponse]:
        path = (request.path or "").strip()

        # 1) 제외 경로 빠른 통과
        for pfx in self.EXCLUDE_PREFIXES:
            if path.startswith(pfx):
                return None

        # 2) staff는 무조건 통과 (운영 편의)
        try:
            u = getattr(request, "user", None)
            if u and getattr(u, "is_staff", False):
                return None
        except Exception:
            pass

        method = (request.method or "GET").upper()

        # 3) 룰 매칭
        rule = None
        for r in self.RULES:
            if method != r.method:
                continue
            # 슬래시 유무 호환: prefix로 잡고, trailing slash도 자연히 포함
            if path.startswith(r.path_prefix):
                rule = r
                break

        if not rule:
            return None

        # 4) query 조건(예: q 있을 때만)
        if rule.require_query_key:
            v = (request.GET.get(rule.require_query_key) or "").strip()
            if not v:
                return None

        actor = _get_actor_key(request)

        # 5) 캐시 키: actor + endpoint + method
        key = f"throt:img:{method}:{rule.path_prefix}:{actor}"

        # add() = 없을 때만 set (원자적)
        ok = cache.add(key, 1, timeout=rule.cooldown_sec)
        if ok:
            return None

        # 6) 차단 응답
        retry_after = rule.cooldown_sec

        # API는 JSON 429
        if path.startswith("/api/"):
            resp = JsonResponse(
                {
                    "ok": False,
                    "error": "too_many_requests",
                    "message": "너무 빠르게 요청했어요. 잠시 후 다시 시도해 주세요.",
                    "retry_after_sec": retry_after,
                },
                status=429,
                json_dumps_params={"ensure_ascii": False},
            )
            resp["Retry-After"] = str(retry_after)
            return resp

        # 페이지는 간단 HTML(템플릿 추가 없이)
        html = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>잠시만요</title>
</head>
<body style="font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; padding:24px;">
  <h2 style="margin:0 0 8px;">잠시만요 🖐️</h2>
  <p style="margin:0 0 14px; line-height:1.5;">
    너무 빠르게 요청했어요. <b>{retry_after}초</b> 뒤에 다시 시도해 주세요.
  </p>
  <button onclick="history.back()" style="padding:10px 14px;">뒤로가기</button>
</body>
</html>"""
        resp = HttpResponse(html, status=429, content_type="text/html; charset=utf-8")
        resp["Retry-After"] = str(retry_after)
        return resp
