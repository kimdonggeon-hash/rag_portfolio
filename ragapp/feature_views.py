# ragapp/feature_views.py
from __future__ import annotations

"""
✅ feature_views.py는 wiring/adapter만 둔다.
- 실제 로직은 ragapp/machine/*_machine.py에만 존재
- urls.py는 기존처럼 feature_views.<view> 를 계속 참조해도 됨
"""

import logging
from importlib import import_module
from typing import Any, Callable, Dict, Tuple

from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseNotFound,
    JsonResponse,
    HttpResponseRedirect,
)

log = logging.getLogger(__name__)

# ────────────────────────────────────────────────
# tiny helpers (lazy import + resolve cache)
# ────────────────────────────────────────────────
_RESOLVED: Dict[Tuple[str, str], Callable[..., HttpResponse]] = {}


def _resolve(module_path: str, fn_name: str) -> Callable[..., HttpResponse]:
    key = (module_path, fn_name)
    fn = _RESOLVED.get(key)
    if fn is not None:
        return fn
    mod = import_module(module_path)
    fn = getattr(mod, fn_name)
    _RESOLVED[key] = fn
    return fn


def _call_page(module_path: str, fn_name: str, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    try:
        fn = _resolve(module_path, fn_name)
    except Exception:
        log.exception("feature_views missing page: %s.%s", module_path, fn_name)
        # ✅ 내부 정보 노출 방지(퍼블릭 페이지 안전)
        return HttpResponseNotFound("not available")
    return fn(request, *args, **kwargs)


def _call_api(module_path: str, fn_name: str, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    try:
        fn = _resolve(module_path, fn_name)
    except Exception:
        log.exception("feature_views missing api: %s.%s", module_path, fn_name)
        # ✅ 내부 정보 노출 방지(퍼블릭 API 안전)
        return JsonResponse(
            {"ok": False, "error": "not available"},
            status=501,
            json_dumps_params={"ensure_ascii": False},
        )
    return fn(request, *args, **kwargs)


# ────────────────────────────────────────────────
# Media (images) — logic lives in media_machine
# ────────────────────────────────────────────────
_MEDIA = "ragapp.machine.media_machine"

# pages
def media_index_view(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    return _call_page(_MEDIA, "media_index_view", request, *args, **kwargs)


def media_search_view(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    return _call_page(_MEDIA, "media_search_view", request, *args, **kwargs)


def media_pending_admin_view(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    return _call_page(_MEDIA, "media_pending_admin_view", request, *args, **kwargs)


def media_rejected_admin_view(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    return _call_page(_MEDIA, "media_rejected_admin_view", request, *args, **kwargs)


def media_approved_admin_view(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    return _call_page(_MEDIA, "media_approved_admin_view", request, *args, **kwargs)


def media_upload_admin_view(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    # ✅ 스태프 전용 업로드(즉시 승격/인덱싱) 화면
    return _call_page(_MEDIA, "media_upload_admin_view", request, *args, **kwargs)


def media_penalties_admin_view(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    # ✅ 선처(정지/제재 해제) 화면
    return _call_page(_MEDIA, "media_penalties_admin_view", request, *args, **kwargs)


# APIs
def api_media_upsert_tags_caption(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    return _call_api(_MEDIA, "api_media_upsert_tags_caption", request, *args, **kwargs)


def api_media_keyword_search(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    return _call_api(_MEDIA, "api_media_keyword_search", request, *args, **kwargs)


def api_media_pending_list(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    return _call_api(_MEDIA, "api_media_pending_list", request, *args, **kwargs)


def api_media_pending_approve(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    return _call_api(_MEDIA, "api_media_pending_approve", request, *args, **kwargs)


def api_media_pending_reject(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    return _call_api(_MEDIA, "api_media_pending_reject", request, *args, **kwargs)


def api_media_rejected_delete(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    return _call_api(_MEDIA, "api_media_rejected_delete", request, *args, **kwargs)


def api_media_approved_remove(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    return _call_api(_MEDIA, "api_media_approved_remove", request, *args, **kwargs)


# ✅ alias: urls.py에서 이름을 이쪽으로 쓰고 싶을 때
def api_user_penalty_list(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    return _call_api(_MEDIA, "api_user_penalty_list", request, *args, **kwargs)


def api_user_penalty_lift(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    return _call_api(_MEDIA, "api_user_penalty_lift", request, *args, **kwargs)


# ────────────────────────────────────────────────
# Table (csv/xlsx) — logic lives in table_machine
# ────────────────────────────────────────────────
_TABLE = "ragapp.machine.table_machine"


def _redirect_keep_qs(request: HttpRequest, target: str) -> HttpResponse:
    qs = request.META.get("QUERY_STRING", "")
    if qs:
        return HttpResponseRedirect(f"{target}?{qs}")
    return HttpResponseRedirect(target)


# ✅ (레거시/안전) 혹시 남아있는 링크가 있으면 admin으로 보내기 (쿼리 보존)
def table_index_view(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    return _redirect_keep_qs(request, "/ragadmin/table/index/")


def table_search_view(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    return _redirect_keep_qs(request, "/ragadmin/table/search/")


# ✅ admin 전용 endpoint는 machine의 실제 구현(이미 staff_required 걸려있음)을 그대로 호출
def table_index_admin_view(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    return _call_page(_TABLE, "table_index_view", request, *args, **kwargs)


def table_search_admin_view(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    return _call_page(_TABLE, "table_search_view", request, *args, **kwargs)


__all__ = [
    # media pages
    "media_index_view",
    "media_search_view",
    "media_pending_admin_view",
    "media_rejected_admin_view",
    "media_approved_admin_view",
    "media_upload_admin_view",
    "media_penalties_admin_view",
    # media apis
    "api_media_upsert_tags_caption",
    "api_media_keyword_search",
    "api_media_pending_list",
    "api_media_pending_approve",
    "api_media_pending_reject",
    "api_media_rejected_delete",
    "api_media_approved_remove",
    "api_user_penalty_list",
    "api_user_penalty_lift",
    # table pages (legacy redirect 포함)
    "table_index_view",
    "table_search_view",
    # table pages (admin-only 공개)
    "table_index_admin_view",
    "table_search_admin_view",
]
