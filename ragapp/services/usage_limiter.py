# ragapp/services/usage_limiter.py
from __future__ import annotations

import hashlib
from typing import Tuple, Dict

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ragapp.models import DailyUsage

_FIELD_MAP = {
    "web": "web_count",
    "rag": "rag_count",
    "pdf": "pdf_count",
    "image": "image_count",
    "table": "table_count",
}

_ADMIN_FIELD = "admin_count"
_ADMIN_CACHE_KEY = "__admin__"


def _client_ip(request) -> str:
    xff_raw = request.META.get("HTTP_X_FORWARDED_FOR") or ""
    xff = [p.strip() for p in xff_raw.split(",") if p.strip()]
    if xff:
        return xff[0]
    return request.META.get("REMOTE_ADDR", "") or "0.0.0.0"


def build_client_key(request) -> str:
    ip = _client_ip(request)
    ua = (request.META.get("HTTP_USER_AGENT") or "")[:80]
    secret = getattr(settings, "LOG_IP_HASH_SECRET", "") or "usage-secret"
    raw = f"{secret}|{ip}|{ua}"
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()


def get_daily_limit(kind: str) -> int:
    if kind == "web":
        return int(getattr(settings, "QA_USAGE_LIMIT_WEB", 0) or 0)
    if kind == "rag":
        return int(getattr(settings, "QA_USAGE_LIMIT_RAG", 0) or 0)
    if kind == "pdf":
        return int(getattr(settings, "QA_USAGE_LIMIT_PDF", 0) or 0)
    if kind == "image":
        return int(getattr(settings, "QA_USAGE_LIMIT_IMAGE", 0) or 0)
    if kind == "table":
        return int(getattr(settings, "QA_USAGE_LIMIT_TABLE", 0) or 0)
    return 0


def _req_usage_cache(request) -> Dict[str, Tuple[bool, int, int]]:
    cache = getattr(request, "_usage_limiter_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        try:
            setattr(request, "_usage_limiter_cache", cache)
        except Exception:
            pass
    return cache


def is_admin_unlimited(request) -> bool:
    user = getattr(request, "user", None)
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))
    )


def bump_admin_usage(request) -> None:
    """
    ✅ staff/superuser가 /admin/, /ragadmin/ 같은 경로로 들어올 때 admin_count +1
    - 요청당 1번만 증가(멱등)
    """
    if not is_admin_unlimited(request):
        return

    cache = _req_usage_cache(request)
    if _ADMIN_CACHE_KEY in cache:
        return

    today = timezone.localdate()
    key = build_client_key(request)

    try:
        with transaction.atomic():
            usage, _ = DailyUsage.objects.select_for_update().get_or_create(
                date=today,
                client_key=key,
                defaults={
                    "web_count": 0,
                    "rag_count": 0,
                    "pdf_count": 0,
                    "image_count": 0,
                    "table_count": 0,
                    "admin_count": 0,
                },
            )

            cur = int(getattr(usage, _ADMIN_FIELD, 0) or 0)
            setattr(usage, _ADMIN_FIELD, cur + 1)

            update_fields = [_ADMIN_FIELD]
            if hasattr(usage, "updated_at"):
                update_fields.append("updated_at")
            usage.save(update_fields=update_fields)
    except Exception:
        # admin 카운트 실패해도 기능 자체는 막지 않음
        pass

    cache[_ADMIN_CACHE_KEY] = (True, 0, 0)


def check_and_increment_usage(request, kind: str) -> Tuple[bool, int, int]:
    """
    하드 제한 핵심 함수.
    - 반환: (allowed, limit, used_after)
    ✅ 요청당 kind별로 1회만 차감(멱등)
    """
    cache = _req_usage_cache(request)
    if kind in cache:
        return cache[kind]

    # ✅ admin은 제한 제외 + 카운트도 안 셈(기능 카운트는 의미없고 DB쓰기 줄이기)
    if is_admin_unlimited(request):
        cache[kind] = (True, 0, 0)
        return cache[kind]

    limit = get_daily_limit(kind)
    if limit <= 0:
        cache[kind] = (True, 0, 0)
        return cache[kind]

    today = timezone.localdate()
    key = build_client_key(request)

    with transaction.atomic():
        usage, _ = DailyUsage.objects.select_for_update().get_or_create(
            date=today,
            client_key=key,
            defaults={
                "web_count": 0,
                "rag_count": 0,
                "pdf_count": 0,
                "image_count": 0,
                "table_count": 0,
                "admin_count": 0,
            },
        )

        field = _FIELD_MAP.get(kind)
        if not field:
            cache[kind] = (True, 0, 0)
            return cache[kind]

        used = int(getattr(usage, field) or 0)
        if used >= limit:
            cache[kind] = (False, limit, used)
            return cache[kind]

        setattr(usage, field, used + 1)

        update_fields = [field]
        if hasattr(usage, "updated_at"):
            update_fields.append("updated_at")

        usage.save(update_fields=update_fields)
        cache[kind] = (True, limit, used + 1)
        return cache[kind]
