# ragapp/views_ops_control.py
from __future__ import annotations

import json
import time
from django.http import JsonResponse, HttpRequest
from django.views.decorators.http import require_POST, require_GET
from django.views.decorators.csrf import csrf_protect
from django.contrib.admin.views.decorators import staff_member_required
from django.core.cache import cache

from ragapp.middleware.ops_control import (
    K_MAINT, K_WRITELOCK, K_SPIKE, K_UPDATED_AT,
    OpsControlMiddleware,
)


def _read_json(request: HttpRequest) -> dict:
    try:
        raw = request.body or b""
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        return {}


def _status_payload() -> dict:
    ops = OpsControlMiddleware.snapshot_ops()
    online = OpsControlMiddleware.snapshot_online()
    return {"ok": True, "ops": ops, "online": online}


def _touch_updated_at() -> None:
    cache.set(K_UPDATED_AT, str(int(time.time())), timeout=3600 * 24 * 30)


@require_GET
@staff_member_required
def ops_status(request: HttpRequest):
    return JsonResponse(
        _status_payload(),
        json_dumps_params={"ensure_ascii": False},
    )


@require_POST
@csrf_protect
@staff_member_required
def ops_set(request: HttpRequest):
    data = _read_json(request)
    target = (data.get("target") or "").strip()
    enabled = data.get("enabled", None)

    if target not in ("maintenance", "writelock", "spike_guard"):
        return JsonResponse({"ok": False, "error": "invalid_target"}, status=400)

    key = {"maintenance": K_MAINT, "writelock": K_WRITELOCK, "spike_guard": K_SPIKE}[target]

    # enabled가 없으면 토글, 있으면 지정값으로 set
    if enabled is None:
        cur = cache.get(key, 0)
        try:
            cur_b = bool(int(cur))
        except Exception:
            cur_b = bool(cur)
        cache.set(key, 0 if cur_b else 1, timeout=3600 * 24 * 30)
    else:
        cache.set(key, 1 if bool(enabled) else 0, timeout=3600 * 24 * 30)

    _touch_updated_at()

    # ✅ 여기서 ops_status(request) 호출하면 다시 @require_GET에 걸려 405가 남
    return JsonResponse(
        _status_payload(),
        json_dumps_params={"ensure_ascii": False},
    )


@require_POST
@csrf_protect
@staff_member_required
def ops_reset_presence(request: HttpRequest):
    # presence reset: index만 지우면 카운트가 0으로 떨어짐
    cache.delete("presence:index:u")
    cache.delete("presence:index:a")
    _touch_updated_at()
    return JsonResponse({"ok": True}, json_dumps_params={"ensure_ascii": False})
