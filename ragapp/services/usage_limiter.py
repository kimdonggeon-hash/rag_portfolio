# ragapp/services/usage_limiter.py
from __future__ import annotations

import hashlib
import re
from typing import Tuple, Dict, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ragapp.models import DailyUsage
from ragapp.services.client_ip import get_client_ip

_FIELD_MAP = {
    "web": "web_count",
    "rag": "rag_count",
    "pdf": "pdf_count",
    "image": "image_count",
    "table": "table_count",
}

_ADMIN_FIELD = "admin_count"
_ADMIN_CACHE_KEY = "__admin__"

# ✅ 기기(브라우저) 단위로 오늘 사용량을 유지하기 위한 장기 쿠키.
# IP만으로 client_key를 만들면 공유기/모뎀 재시작 등으로 공인 IP가 바뀔 때마다
# 완전히 새로운 client_key가 되어 "사용량이 초기화된 것처럼" 보이는 문제가 있었음.
# 쿠키가 있으면 그걸 우선 쓰고, 없을 때만 IP+UA로 폴백한다(최초 방문 등).
CID_COOKIE_NAME = "dg_cid"
_CID_RE = re.compile(r"^[a-f0-9]{32}$")


def _client_ip(request) -> str:
    # XFF 맨 왼쪽은 클라이언트가 위조할 수 있어서 IP 한도가 뚫린다.
    # 신뢰 가능한 위치를 고르는 로직은 client_ip 모듈로 일원화했다.
    return get_client_ip(request)


def get_cookie_cid(request) -> Optional[str]:
    cid = (request.COOKIES.get(CID_COOKIE_NAME) or "").strip().lower()
    return cid if _CID_RE.match(cid) else None


def build_client_key(request) -> str:
    secret = getattr(settings, "LOG_IP_HASH_SECRET", "") or "usage-secret"
    cid = get_cookie_cid(request)
    if cid:
        raw = f"{secret}|cid:{cid}"
    else:
        ip = _client_ip(request)
        ua = (request.META.get("HTTP_USER_AGENT") or "")[:80]
        raw = f"{secret}|{ip}|{ua}"
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()


def build_client_keys(request) -> list[str]:
    """
    ✅ 오늘 사용량 조회/차감에 쓸 후보 키들(쿠키 기반 + IP 기반) 전부.
    쿠키 하나만 쓰면 "브라우저를 바꾸면 다른 사람으로 보이는" 문제가 있고,
    IP만 쓰면 "IP가 바뀌면 리셋되는" 문제가 있어서, 둘 다 후보로 두고
    사용량은 둘 중 더 큰 값을 기준으로 판단 + 둘 다 같이 갱신한다.
    이러면 (같은 기기, IP만 바뀜)과 (같은 IP, 브라우저만 바뀜) 둘 다
    같은 사람으로 취급되어 사용량이 이어진다.

    ⚠️ UA(User-Agent)는 여기서 일부러 뺐다. 사파리/크롬처럼 같은 기기에서도
    브라우저마다 UA 문자열 자체가 완전히 다르기 때문에, IP+UA로 묶으면
    "같은 와이파이인데 브라우저만 다르면" 여전히 다른 사람으로 보였다.
    IP만으로 묶어야 브라우저가 달라도 진짜로 합쳐진다(대신 같은 공인 IP를
    쓰는 다른 사람과 섞일 수 있지만, 개인 포트폴리오 수준 한도라 감수).
    """
    return [k for k, _kind in build_client_keys_with_kind(request)]


def build_client_keys_with_kind(request) -> list[tuple[str, str]]:
    """
    build_client_keys()와 같지만 각 키가 쿠키 기반인지 IP 기반인지도 같이 준다.
    DailyUsage.key_kind에 기록해서 통계에서 중복 집계를 피하는 데 쓴다.
    """
    secret = getattr(settings, "LOG_IP_HASH_SECRET", "") or "usage-secret"
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    cid = get_cookie_cid(request)
    if cid:
        k = hashlib.sha256(f"{secret}|cid:{cid}".encode("utf-8", "ignore")).hexdigest()
        out.append((k, "cid"))
        seen.add(k)

    ip = _client_ip(request)
    ip_key = hashlib.sha256(f"{secret}|ip:{ip}".encode("utf-8", "ignore")).hexdigest()
    if ip_key not in seen:
        out.append((ip_key, "ip"))

    return out


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

    field = _FIELD_MAP.get(kind)
    if not field:
        cache[kind] = (True, 0, 0)
        return cache[kind]

    today = timezone.localdate()
    keyed = build_client_keys_with_kind(request)

    with transaction.atomic():
        rows = []
        for k, key_kind in keyed:
            row, _ = DailyUsage.objects.select_for_update().get_or_create(
                date=today,
                client_key=k,
                defaults={
                    "key_kind": key_kind,
                    "web_count": 0,
                    "rag_count": 0,
                    "pdf_count": 0,
                    "image_count": 0,
                    "table_count": 0,
                    "admin_count": 0,
                },
            )
            # 기존(마이그레이션 이전에 생긴) 행은 key_kind가 비어 있으니 채워준다
            if not getattr(row, "key_kind", ""):
                row.key_kind = key_kind
                row.save(update_fields=["key_kind"])
            rows.append(row)

        used = max((int(getattr(r, field) or 0) for r in rows), default=0)
        if used >= limit:
            cache[kind] = (False, limit, used)
            return cache[kind]

        new_used = used + 1
        update_fields = [field]
        if hasattr(rows[0], "updated_at"):
            update_fields.append("updated_at")

        for r in rows:
            setattr(r, field, new_used)
            r.save(update_fields=update_fields)

        cache[kind] = (True, limit, new_used)
        return cache[kind]


def refund_usage(request, kind: str) -> None:
    """
    check_and_increment_usage()로 차감한 사용량 1회를 되돌린다.

    실제로는 AI 호출이 실패해서 로컬 폴백 등으로 대신 응답한 경우처럼,
    사용자의 오늘 사용 횟수를 소모시키면 안 되는 상황에 사용한다.
    - admin/무제한 케이스는 애초에 차감하지 않았으므로 아무 것도 하지 않는다.
    - 이번 요청에서 차감이 실제로 일어나지 않았다면(한도 초과 등) 아무 것도 하지 않는다.
    """
    if is_admin_unlimited(request):
        return

    cache = _req_usage_cache(request)
    cached = cache.get(kind)
    if not cached or not cached[0]:
        return

    limit = get_daily_limit(kind)
    if limit <= 0:
        return

    field = _FIELD_MAP.get(kind)
    if not field:
        return

    today = timezone.localdate()
    keys = build_client_keys(request)

    try:
        with transaction.atomic():
            for k in keys:
                usage = DailyUsage.objects.select_for_update().filter(date=today, client_key=k).first()
                if usage is None:
                    continue
                used = int(getattr(usage, field) or 0)
                if used <= 0:
                    continue
                setattr(usage, field, used - 1)
                update_fields = [field]
                if hasattr(usage, "updated_at"):
                    update_fields.append("updated_at")
                usage.save(update_fields=update_fields)
    except Exception:
        return

    allowed, _, used_after = cached
    cache[kind] = (allowed, limit, max(0, used_after - 1))
