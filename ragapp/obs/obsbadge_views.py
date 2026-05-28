# ragapp/obs/obsbadge_views.py
from __future__ import annotations

import json
from typing import Any

from django.http import JsonResponse, HttpRequest, HttpResponseNotFound
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_protect


SESSION_KEY = "admin_obs_enabled"


def _is_staff(request: HttpRequest) -> bool:
    u = getattr(request, "user", None)
    return bool(
        getattr(u, "is_authenticated", False)
        and (getattr(u, "is_staff", False) or getattr(u, "is_superuser", False))
    )


def _get_enabled(request: HttpRequest) -> bool:
    return bool(request.session.get(SESSION_KEY, False))


def _set_enabled(request: HttpRequest, enabled: bool) -> None:
    request.session[SESSION_KEY] = bool(enabled)
    request.session.modified = True


@csrf_protect
@require_http_methods(["GET"])
def obsbadge_status(request: HttpRequest):
    # ✅ 스태프만(비로그인은 404로 은닉)
    if not _is_staff(request):
        return HttpResponseNotFound("Not Found")

    return JsonResponse({"ok": True, "enabled": _get_enabled(request)}, json_dumps_params={"ensure_ascii": False})


@csrf_protect
@require_http_methods(["POST"])
def obsbadge_toggle(request: HttpRequest):
    # ✅ 스태프만(비로그인은 404로 은닉)
    if not _is_staff(request):
        return HttpResponseNotFound("Not Found")

    enabled: Any = None
    try:
        if (request.content_type or "").lower().startswith("application/json"):
            body = request.body.decode("utf-8") if request.body else "{}"
            data = json.loads(body)
            enabled = data.get("enabled", None)
    except Exception:
        enabled = None

    # form POST도 허용(혹시 위젯이 form 방식이면)
    if enabled is None:
        enabled = request.POST.get("enabled", None)

    # ✅ enabled가 명시되면 set, 아니면 toggle
    if enabled is None:
        new_val = (not _get_enabled(request))
    else:
        s = str(enabled).strip().lower()
        new_val = s in ("1", "true", "t", "yes", "y", "on")

    _set_enabled(request, new_val)
    return JsonResponse({"ok": True, "enabled": new_val}, json_dumps_params={"ensure_ascii": False})
