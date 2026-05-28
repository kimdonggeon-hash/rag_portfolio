# ragapp/middleware_consent.py
from __future__ import annotations

import hashlib
from typing import Optional

from django.conf import settings
from django.utils import timezone


def _get_client_ip(request) -> str:
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or ""


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()


def _client_key(request) -> str:
    # 네 프로젝트에 dgcid 같은 클라이언트 쿠키가 있으면 그걸 우선
    dgcid = request.COOKIES.get("dgcid", "") or request.COOKIES.get("client_id", "")
    ua = request.META.get("HTTP_USER_AGENT", "")[:200]
    ip = _get_client_ip(request)
    base = f"{dgcid}|{ip}|{ua}"
    return _sha256(base)


def _cookie_truthy(val: Optional[str]) -> bool:
    if val is None:
        return False
    v = str(val).strip().lower()
    return v in ("1", "true", "yes", "y", "ok", "agree", "accepted", "on")


class ConsentCaptureMiddleware:
    """
    - CONSENT_COOKIE_NAMES 중 하나라도 truthy면 '동의 완료'로 간주
    - CONSENT_LOGGED_COOKIE가 없을 때만 DB에 1회 기록 후 logged 쿠키 세팅
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        try:
            cookie_names = getattr(settings, "CONSENT_COOKIE_NAMES", ["dg_consent"])
            logged_cookie = getattr(settings, "CONSENT_LOGGED_COOKIE", "dg_consent_logged_v1")
            if request.COOKIES.get(logged_cookie):
                return response

            consent_val = None
            for name in cookie_names:
                if name in request.COOKIES:
                    consent_val = request.COOKIES.get(name)
                    break

            if not _cookie_truthy(consent_val):
                return response

            # 지장 없이 실패해도 서비스는 계속 돌아야 하므로 try/except
            from ragapp.legal_models import ConsentEvent  # 아래 4번 파일

            ConsentEvent.objects.create(
                client_key=_client_key(request),
                user=request.user if getattr(request, "user", None) and request.user.is_authenticated else None,
                consent_type=getattr(settings, "CONSENT_LOG_TYPE_DEFAULT", "service"),
                policy_version=getattr(settings, "CONSENT_POLICY_VERSION", "v1"),
                given_at=timezone.now(),
                path=(request.path or "")[:300],
                user_agent=(request.META.get("HTTP_USER_AGENT", "") or "")[:500],
                ip_hash=_sha256(_get_client_ip(request) or ""),
                meta={
                    "cookie_name": name if "name" in locals() else None,
                },
            )

            # 중복 기록 방지용 쿠키(너무 길게 둘 필요는 없음)
            response.set_cookie(
                logged_cookie,
                "1",
                max_age=60 * 60 * 24 * 400,  # 400일
                secure=not bool(getattr(settings, "DEBUG", False)),
                httponly=True,
                samesite="Lax",
            )
        except Exception:
            # 절대 사용자 요청을 망치면 안 됨(로그만 남기고 지나가게)
            return response

        return response
