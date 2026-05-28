# ragapp/views_admin_obs_badge.py
from __future__ import annotations

import json
import os
import platform
from datetime import datetime, timezone

import django
from django.conf import settings
from django.http import JsonResponse, HttpRequest
from django.views.decorators.http import require_POST, require_GET
from django.views.decorators.csrf import csrf_protect
from django.contrib.admin.views.decorators import staff_member_required


SESSION_ENABLED = "dg_obs_enabled"
SESSION_CFG = "dg_obs_cfg"
SESSION_HISTORY = "dg_obs_history"


def _read_json(request: HttpRequest) -> dict:
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return {}


def _get_enabled(request: HttpRequest) -> bool:
    return bool(request.session.get(SESSION_ENABLED, False))


def _get_cfg(request: HttpRequest) -> dict:
    cfg = request.session.get(SESSION_CFG, None)
    return cfg if isinstance(cfg, dict) else {}


def _get_history(request: HttpRequest) -> list:
    h = request.session.get(SESSION_HISTORY, None)
    return h if isinstance(h, list) else []


@require_GET
@csrf_protect
@staff_member_required
def obsbadge_status(request: HttpRequest):
    """
    ✅ JS가 최초 로드 시 현재 상태(enabled/cfg/history_len)를 동기화하는 용도
    """
    return JsonResponse(
        {
            "ok": True,
            "enabled": _get_enabled(request),
            "cfg": _get_cfg(request),
            "history_len": len(_get_history(request)),
        },
        json_dumps_params={"ensure_ascii": False},
    )


@require_POST
@csrf_protect
@staff_member_required
def obsbadge_toggle(request: HttpRequest):
    data = _read_json(request)

    # JSON에 enabled가 오면 set, 아니면 toggle
    if "enabled" in data:
        request.session[SESSION_ENABLED] = bool(data["enabled"])
    else:
        request.session[SESSION_ENABLED] = (not _get_enabled(request))

    request.session.modified = True
    return JsonResponse(
        {"ok": True, "enabled": _get_enabled(request)},
        json_dumps_params={"ensure_ascii": False},
    )


@require_POST
@csrf_protect
@staff_member_required
def obsbadge_config(request: HttpRequest):
    data = _read_json(request)
    cfg = data.get("cfg")
    if not isinstance(cfg, dict):
        return JsonResponse(
            {"ok": False, "error": "invalid_cfg"},
            status=400,
            json_dumps_params={"ensure_ascii": False},
        )

    out: dict = {}
    out["pos"] = cfg.get("pos", "br")

    try:
        out["compact"] = 1 if int(cfg.get("compact", 0)) == 1 else 0
    except Exception:
        out["compact"] = 1 if bool(cfg.get("compact", 0)) else 0

    try:
        out["history_max"] = int(cfg.get("history_max", 10) or 10)
        out["history_max"] = max(3, min(30, out["history_max"]))
    except Exception:
        out["history_max"] = 10

    fields = cfg.get("fields") or {}
    norm_fields = {}
    if isinstance(fields, dict):
        for k, v in fields.items():
            try:
                norm_fields[k] = 1 if int(v) == 1 else 0
            except Exception:
                norm_fields[k] = 1 if bool(v) else 0
    out["fields"] = norm_fields

    request.session[SESSION_CFG] = out
    request.session.modified = True
    return JsonResponse(
        {"ok": True, "cfg": out},
        json_dumps_params={"ensure_ascii": False},
    )


@require_POST
@csrf_protect
@staff_member_required
def obsbadge_history_clear(request: HttpRequest):
    request.session[SESSION_HISTORY] = []
    request.session.modified = True
    return JsonResponse({"ok": True}, json_dumps_params={"ensure_ascii": False})


@require_GET
@csrf_protect
@staff_member_required
def obsbadge_health(request: HttpRequest):
    env = getattr(settings, "ENV_NAME", "") or ("prod" if not settings.DEBUG else "dev")
    rev = getattr(settings, "K_REVISION", "") or os.environ.get("K_REVISION", "")
    host = os.environ.get("HOSTNAME", "")

    data = {
        "ok": True,
        "server_time_utc": datetime.now(timezone.utc).isoformat(),
        "env": env,
        "revision": rev,
        "host": host,
        "django": django.get_version(),
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
    return JsonResponse(data, json_dumps_params={"ensure_ascii": False})
