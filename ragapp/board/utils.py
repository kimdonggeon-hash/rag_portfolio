from __future__ import annotations

import hashlib

from django.conf import settings

from ragapp.services.usage_limiter import get_cookie_cid


def _request_ip(request) -> str:
    ip = (
        request.META.get("HTTP_CF_CONNECTING_IP")
        or request.META.get("HTTP_X_FORWARDED_FOR")
        or request.META.get("REMOTE_ADDR")
        or ""
    )
    return (ip.split(",")[0] if ip else "").strip()


def build_ip_fp_from_request(request) -> str:
    """쿠키/세션과 무관하게 IP만으로 계산되는 FP.
    같은 기기라도 쿠키가 브라우저마다 달라서(사파리↔크롬) cid 기반 fp는 서로 다르게
    나온다 — 이 값은 두 브라우저에서 항상 동일하므로 차단 확인/적용의 "다리" 역할을 한다.
    """
    secret = getattr(settings, "LOG_IP_HASH_SECRET", "") or "board-fp-secret"
    raw = f"{secret}|ip:{_request_ip(request)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def build_fp_from_request(request) -> str:
    """게시판 차단(FP) 식별자.
    ✅ 쿠키(dg_cid) 우선 — IP+UA만으로 만들면 같은 기기에서도 브라우저(사파리↔크롬)만
    바꾸거나 공유기 재시작으로 IP가 바뀌면 다른 사람으로 취급되어 차단이 쉽게 우회됐다.
    쿠키가 없을 때만 IP로 폴백한다(UA는 쓰지 않음).
    """
    cid = get_cookie_cid(request)
    if cid:
        secret = getattr(settings, "LOG_IP_HASH_SECRET", "") or "board-fp-secret"
        raw = f"{secret}|cid:{cid}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return build_ip_fp_from_request(request)


def build_fp_candidates_from_request(request) -> list[str]:
    """차단 확인/적용에 쓸 후보 FP 전부(쿠키 기반 + IP 기반).
    ✅ 브라우저를 바꿔도(사파리↔크롬) IP 기반 fp는 같으므로, 차단을 두 값 모두에
    걸어두면(자동 차단 시점) 다른 브라우저로 접속해도 계속 차단 상태가 유지된다."""
    keys: list[str] = []
    cid_fp = build_fp_from_request(request)
    keys.append(cid_fp)
    ip_fp = build_ip_fp_from_request(request)
    if ip_fp not in keys:
        keys.append(ip_fp)
    return keys
