# ragapp/views_admin_ops_control.py
from __future__ import annotations

import json
import time
from datetime import datetime, timezone as dt_timezone
from typing import Any, Dict

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import csrf_protect


OPS_FLAGS_KEY = getattr(settings, "OPS_FLAGS_CACHE_KEY", "ops:flags:v1")
OPS_PRESENCE_KEY = getattr(settings, "OPS_PRESENCE_CACHE_KEY", "ops:presence:v1")
PRESENCE_TTL_SEC = int(getattr(settings, "OPS_PRESENCE_TTL_SEC", 90))


def _now_iso_utc() -> str:
    return datetime.now(dt_timezone.utc).isoformat()


def _read_json(request: HttpRequest) -> dict:
    try:
        return json.loads((request.body or b"{}").decode("utf-8") or "{}")
    except Exception:
        return {}


def _staff_api_required(view_func):
    """
    admin 데코레이터(staff_member_required)는 기본이 'redirect'라서
    fetch(JSON)에서 HTML 로그인 페이지가 와버릴 수 있음.
    여기서는 JSON 403으로 고정.
    """
    def _wrapped(request: HttpRequest, *args, **kwargs):
        user = getattr(request, "user", None)
        if not (getattr(user, "is_authenticated", False) and getattr(user, "is_staff", False)):
            return JsonResponse(
                {"ok": False, "error": "forbidden", "message": "staff only"},
                status=403,
                json_dumps_params={"ensure_ascii": False},
            )
        return view_func(request, *args, **kwargs)

    return _wrapped


def _normalize_flags(raw: Any) -> Dict[str, bool]:
    d: Dict[str, Any] = raw if isinstance(raw, dict) else {}
    def _b(v: Any) -> bool:
        try:
            if isinstance(v, str):
                return v.strip().lower() in ("1", "true", "yes", "y", "on")
            return bool(int(v)) if isinstance(v, (int, float)) else bool(v)
        except Exception:
            return bool(v)

    return {
        "maintenance": _b(d.get("maintenance", False)),
        "write_lock": _b(d.get("write_lock", False)),
        "spike_mode": _b(d.get("spike_mode", False)),
    }


def _get_flags() -> Dict[str, bool]:
    return _normalize_flags(cache.get(OPS_FLAGS_KEY) or {})


def _set_flags(new_flags: Dict[str, bool]) -> None:
    # 30일 정도 유지(운영 값이니 길게)
    cache.set(OPS_FLAGS_KEY, dict(new_flags), timeout=60 * 60 * 24 * 30)


def _get_presence_map() -> Dict[str, int]:
    m = cache.get(OPS_PRESENCE_KEY) or {}
    return m if isinstance(m, dict) else {}


def _active_sessions_count() -> int:
    now = int(time.time())
    cutoff = now - int(PRESENCE_TTL_SEC)
    m = _get_presence_map()
    n = 0
    for _k, ts in m.items():
        try:
            t = int(ts)
        except Exception:
            continue
        if t >= cutoff:
            n += 1
    return int(n)


@require_GET
@_staff_api_required
def ops_status_api(request: HttpRequest):
    flags = _get_flags()
    active = _active_sessions_count()

    return JsonResponse(
        {
            "ok": True,
            "flags": flags,
            "online": {"active_sessions": active, "ttl_sec": int(PRESENCE_TTL_SEC)},
            "updated_at": _now_iso_utc(),
        },
        json_dumps_params={"ensure_ascii": False},
    )


@require_POST
@csrf_protect
@_staff_api_required
def ops_set_api(request: HttpRequest):
    data = _read_json(request)

    # payload는 JS가 {maintenance, write_lock, spike_mode}로 보냄
    cur = _get_flags()
    for k in ("maintenance", "write_lock", "spike_mode"):
        if k in data:
            cur[k] = bool(data[k])

    _set_flags(cur)
    return JsonResponse({"ok": True, "flags": cur}, json_dumps_params={"ensure_ascii": False})


@require_POST
@csrf_protect
@_staff_api_required
def ops_reset_presence_api(request: HttpRequest):
    # 접속자 집계 캐시 초기화
    cache.set(OPS_PRESENCE_KEY, {}, timeout=60 * 60)
    return JsonResponse({"ok": True}, json_dumps_params={"ensure_ascii": False})
