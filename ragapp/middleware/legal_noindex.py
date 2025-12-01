# ragapp/middleware/legal_noindex.py
from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest, HttpResponse


def _set_if_absent(resp: HttpResponse, key: str, value: str) -> None:
    # Django HttpResponse는 dict-like 헤더 설정을 지원
    if key not in resp:
        resp[key] = value


class LegalSecurityHeadersMiddleware:
    """
    - X-Robots-Tag:   NOINDEX_ENABLED가 참이면 'noindex, noarchive'를 기본 부여
                      (API/정적 파일은 기본 제외, 허용/차단 목록으로 세밀 제어)
    - Referrer-Policy: 기본 'strict-origin-when-cross-origin'
    - X-Content-Type-Options: nosniff
    - X-Frame-Options: settings.X_FRAME_OPTIONS 우선, 없으면 'DENY'
    - Permissions-Policy: settings.PERMISSIONS_POLICY 있으면 전달
    - robots.txt: noindex 활성 시 'Disallow: /'를 반환(설정 값으로 커스터마이즈 가능)

    ▶ settings.py에서 조절 가능한 키(없으면 안전한 기본값 사용)
        NOINDEX_ENABLED          (기본: DEBUG가 True일 때만 True)
        NOINDEX_SKIP_PREFIXES    (기본: ["/api/"])
        INDEX_ALLOWLIST          (기본: [])
        NOINDEX_PATHS            (기본: [])
        REFERRER_POLICY          (기본: "strict-origin-when-cross-origin")
        PERMISSIONS_POLICY       (기본: 미설정)
        STATIC_URL               (정적 경로 기본 제외, 기본: settings.STATIC_URL or "/static/")
        X_FRAME_OPTIONS          (기본: "DENY")
        NOINDEX_ROBOTS_BODY      (기본: "User-agent: *\\nDisallow: /")
    """

    def __init__(self, get_response):
        self.get_response = get_response

        # --- 토글/정책값 로드(안전한 기본)
        self.noindex_enabled: bool = bool(
            getattr(settings, "NOINDEX_ENABLED", bool(getattr(settings, "DEBUG", True)))
        )
        self.referrer_policy: str = getattr(
            settings, "REFERRER_POLICY", "strict-origin-when-cross-origin"
        )
        self.permissions_policy: str | None = getattr(settings, "PERMISSIONS_POLICY", None)

        # 프레임 정책은 Django의 XFrameOptionsMiddleware와 충돌하지 않도록 "없을 때만" 세팅
        self.frame_options_default: str = getattr(settings, "X_FRAME_OPTIONS", "DENY")

        # --- 경로 제어 파라미터
        self.static_url: str = getattr(settings, "STATIC_URL", "/static/")
        # noindex 스킵 기본: API
        self.skip_prefixes: tuple[str, ...] = tuple(
            getattr(settings, "NOINDEX_SKIP_PREFIXES", ("/api/",))
        )
        # 명시 허용/차단 목록
        self.allowlist: set[str] = set(getattr(settings, "INDEX_ALLOWLIST", []) or [])
        self.denylist: set[str] = set(getattr(settings, "NOINDEX_PATHS", []) or [])

        # robots.txt 응답 본문
        self.robots_body: str = getattr(
            settings, "NOINDEX_ROBOTS_BODY", "User-agent: *\nDisallow: /"
        )

    # Django 호출형 미들웨어
    def __call__(self, request: HttpRequest) -> HttpResponse:
        # noindex가 활성화된 경우 robots.txt 직접 응답
        if self.noindex_enabled and request.path.rstrip("/") == "/robots.txt":
            return HttpResponse(self.robots_body, content_type="text/plain")
        response = self.get_response(request)
        return self.process_response(request, response)

    # 응답 후 헤더 주입
    def process_response(self, request: HttpRequest, response: HttpResponse) -> HttpResponse:
        path = request.path or "/"

        # --- 1) noindex 적용 여부 계산
        apply_noindex = False
        if self.noindex_enabled:
            apply_noindex = True

            # 정적 파일/스킵 프리픽스는 noindex 제외
            if self.static_url and path.startswith(self.static_url):
                apply_noindex = False
            elif any(path.startswith(pfx) for pfx in self.skip_prefixes):
                apply_noindex = False

            # 허용목록은 항상 인덱싱 허용
            if path in self.allowlist:
                apply_noindex = False

            # 차단목록은 무조건 noindex
            if path in self.denylist:
                apply_noindex = True

        # 실제 헤더 주입(X-Robots-Tag)
        if apply_noindex:
            _set_if_absent(response, "X-Robots-Tag", "noindex, noarchive")

        # --- 2) 보안/프라이버시 헤더
        _set_if_absent(response, "Referrer-Policy", self.referrer_policy)
        _set_if_absent(response, "X-Content-Type-Options", "nosniff")
        # X-Frame-Options: Django 기본 미들웨어가 이미 넣었을 수 있으니 없을 때만
        _set_if_absent(response, "X-Frame-Options", self.frame_options_default)
        if self.permissions_policy:
            _set_if_absent(response, "Permissions-Policy", self.permissions_policy)

        return response


# ─────────────────────────────────────────────────────────────
# 뒤호환(설정에 NoIndexMiddleware로 등록해도 동작)
# ─────────────────────────────────────────────────────────────
NoIndexMiddleware = LegalSecurityHeadersMiddleware
