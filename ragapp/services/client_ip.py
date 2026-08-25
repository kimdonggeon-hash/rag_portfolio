# ragapp/services/client_ip.py
"""
신뢰할 수 있는 클라이언트 IP 추출.

⚠️ X-Forwarded-For(XFF)는 클라이언트가 직접 보낼 수 있는 헤더다.
Google 프론트엔드/로드밸런서는 클라이언트가 보낸 값을 지우지 않고
"오른쪽에 덧붙이기" 때문에, 맨 왼쪽 값을 그대로 쓰면 요청마다 임의의 IP를
위조할 수 있고 IP 기반 제한(사용량 한도, 레이트리밋, 차단)이 전부 무력화된다.

    X-Forwarded-For: <클라이언트가 위조한 값들>, <실제 클라이언트 IP>, <프록시>
                     └──── 신뢰 불가 ────┘      └───── 여기부터 신뢰 ─────┘

그래서 왼쪽이 아니라 "오른쪽에서부터" 신뢰하는 프록시 홉 수만큼 건너뛴
위치를 고른다. 홉 수는 TRUSTED_PROXY_HOPS로 조정한다.
(Cloud Run + Google 프론트엔드 구성은 1)

홉 수가 실제보다 크면 프록시 IP를 클라이언트로 오인해서 "모두가 한 사람"이
되고, 작으면 위조에 다시 뚫린다. 값을 바꿀 때는 아래 verify_hint()로
실제 XFF가 어떻게 들어오는지 먼저 확인할 것.
"""
from __future__ import annotations

import ipaddress
import os
from typing import Optional

from django.conf import settings

FALLBACK_IP = "0.0.0.0"


def _trusted_proxy_hops() -> int:
    raw = os.environ.get("TRUSTED_PROXY_HOPS")
    if raw is None:
        raw = getattr(settings, "TRUSTED_PROXY_HOPS", 1)
    try:
        hops = int(str(raw).strip())
    except (TypeError, ValueError):
        return 1
    return hops if hops >= 0 else 0


def _normalize(raw: str) -> Optional[str]:
    """
    "1.2.3.4:5678", "[2001:db8::1]:443", "2001:db8::1" 같은 형태를 IP만 남긴다.
    유효한 IP가 아니면 None.
    """
    s = (raw or "").strip()
    if not s:
        return None

    # [IPv6]:port
    if s.startswith("["):
        end = s.find("]")
        if end > 0:
            s = s[1:end]
    # IPv4:port (콜론이 하나뿐이면 포트가 붙은 IPv4로 본다)
    elif s.count(":") == 1:
        s = s.split(":", 1)[0]

    try:
        return str(ipaddress.ip_address(s))
    except ValueError:
        return None


def get_client_ip(request) -> str:
    """
    프록시 뒤에서도 위조되지 않는 클라이언트 IP.

    XFF가 없으면(로컬 개발 등) REMOTE_ADDR를 쓴다.
    """
    xff_raw = request.META.get("HTTP_X_FORWARDED_FOR") or ""
    parts = [p for p in (_normalize(p) for p in xff_raw.split(",")) if p]

    if parts:
        # 오른쪽에서 신뢰 홉 수만큼 건너뛴 위치. 목록이 더 짧으면 맨 왼쪽으로 고정.
        idx = len(parts) - 1 - _trusted_proxy_hops()
        if idx < 0:
            idx = 0
        return parts[idx]

    return _normalize(request.META.get("REMOTE_ADDR") or "") or FALLBACK_IP


def verify_hint(request) -> dict:
    """
    TRUSTED_PROXY_HOPS 값을 맞출 때 쓰는 진단용 정보.
    운영에서 상시 로깅하지 말 것(원본 IP가 그대로 들어있다).
    """
    xff_raw = request.META.get("HTTP_X_FORWARDED_FOR") or ""
    parts = [p.strip() for p in xff_raw.split(",") if p.strip()]
    return {
        "xff_raw": xff_raw,
        "xff_parts": parts,
        "remote_addr": request.META.get("REMOTE_ADDR") or "",
        "trusted_proxy_hops": _trusted_proxy_hops(),
        "picked": get_client_ip(request),
    }
