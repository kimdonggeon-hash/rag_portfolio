from __future__ import annotations

import hashlib


def build_fp_from_request(request) -> str:
    ip = (
        request.META.get("HTTP_CF_CONNECTING_IP")
        or request.META.get("HTTP_X_FORWARDED_FOR")
        or request.META.get("REMOTE_ADDR")
        or ""
    )
    ip = (ip.split(",")[0] if ip else "").strip()
    ua = request.META.get("HTTP_USER_AGENT") or ""
    return hashlib.sha1(f"{ip}|{ua}".encode("utf-8")).hexdigest()[:12]
