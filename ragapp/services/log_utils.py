# ragapp/services/log_utils.py
from __future__ import annotations

import hmac
import hashlib
import logging
from typing import Optional, Dict, Any
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

log = logging.getLogger(__name__)

def _hash_ip(ip: str) -> str:
    secret = (getattr(settings, "LOG_IP_HASH_SECRET", "") or "").encode("utf-8")
    if not secret or not ip:
        return ip or ""
    return hmac.new(secret, ip.encode("utf-8"), hashlib.sha256).hexdigest()[:24]

def _client_ip_from_request(request) -> str:
    try:
        xff = request.META.get("HTTP_X_FORWARDED_FOR")
        if xff:
            ip = xff.split(",")[0].strip()
        else:
            ip = request.META.get("REMOTE_ADDR", "")
        return ip
    except Exception:
        return ""

def log_query(request, query: str, *, context: Optional[Dict[str, Any]] = None) -> None:
    """
    사용자 질의/행위를 로깅.
    - LOG_IP_HASHED=1이면 IP를 해시하여 저장
    - 모델이 없으면 안전하게 패스
    """
    ip = _client_ip_from_request(request) if request is not None else ""
    if getattr(settings, "LOG_IP_HASHED", False):
        ip = _hash_ip(ip)

    payload = {
        "ip": ip,
        "query": (query or "")[:2000],
        "extra": (context or {}),
        "created_at": timezone.now(),
    }

    try:
        # 네가 쓰는 모델이 있으면 우선 시도
        try:
            from ragapp.models import ChatQueryLog  # type: ignore
            ChatQueryLog.objects.create(
                ip=payload["ip"],
                query=payload["query"],
                extra=payload["extra"],
                created_at=payload["created_at"],
            )
        except Exception:
            # 대체 모델 시도
            try:
                from ragapp.models import MyLog  # type: ignore
                MyLog.objects.create(
                    ip=payload["ip"],
                    message=payload["query"],
                    meta=payload["extra"],
                    created_at=payload["created_at"],
                )
            except Exception:
                # 모델이 없거나 에러면 콘솔 로깅만
                log.info("query_log %s", payload)
    except Exception as e:
        log.warning("log_query failed: %s", e)

def purge_old_logs() -> int:
    """
    보관기간(RETENTION_DAYS) 초과 로그를 삭제.
    - 모델이 없으면 0 반환
    """
    days = int(getattr(settings, "RETENTION_DAYS", 0) or 0)
    if days <= 0:
        return 0
    cutoff = timezone.now() - timedelta(days=days)
    deleted = 0

    try:
        from ragapp.models import ChatQueryLog  # type: ignore
        qs = ChatQueryLog.objects.filter(created_at__lt=cutoff)
        deleted += qs.count()
        qs.delete()
    except Exception:
        pass

    try:
        from ragapp.models import MyLog  # type: ignore
        qs = MyLog.objects.filter(created_at__lt=cutoff)
        deleted += qs.count()
        qs.delete()
    except Exception:
        pass

    return deleted
