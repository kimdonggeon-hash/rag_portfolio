# ragapp/decorators.py
from __future__ import annotations

from functools import wraps
from typing import Any, Callable, Dict, Optional

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render

from ragapp.services.usage_limiter import check_and_increment_usage


def _wants_json(request: HttpRequest) -> bool:
    accept = (request.META.get("HTTP_ACCEPT") or "").lower()
    xrw = (request.META.get("HTTP_X_REQUESTED_WITH") or "").lower()
    return ("application/json" in accept) or (xrw == "xmlhttprequest") or request.path.startswith("/api/")


def _render_quota_block(
    request: HttpRequest,
    *,
    kind: str,
    msg: str,
    status: int,
    limit: int = 0,
    used: int = 0,
) -> HttpResponse:
    if _wants_json(request):
        return JsonResponse(
            {"ok": False, "error": msg, "code": "limit_exceeded", "kind": kind, "limit": limit, "used": used},
            status=status,
            json_dumps_params={"ensure_ascii": False},
        )

    # ✅ 화면 렌더(템플릿 변수명 차이 흡수)
    if kind == "image":
        q = (request.GET.get("q") or "").strip()
        size = int(request.GET.get("size") or 12)
        page = int(request.GET.get("page") or 1)
        k = int(request.GET.get("k") or 120)
        ctx = {
            "q": q, "size": size, "page": page, "k": k,
            "hits": [], "has_prev": False, "has_next": False,
            "error": msg,  # ✅ media_search.html 쪽은 보통 error
            "quota": {"code": "limit_exceeded", "kind": kind, "limit": limit, "used": used},
        }
        return render(request, "ragapp/media_search.html", ctx, status=status)

    if kind == "table":
        q = (request.GET.get("q") or "").strip()
        size = int(request.GET.get("size") or 12)
        page = int(request.GET.get("page") or 1)
        k = int(request.GET.get("k") or 200)
        table = (request.GET.get("table") or "").strip()
        group_by = (request.GET.get("group_by") or "").strip()
        agg_field = (request.GET.get("agg_field") or "").strip()
        agg = (request.GET.get("agg") or "").strip().lower()

        ctx = {
            "q": q, "size": size, "page": page, "k": k,
            "table": table, "group_by": group_by, "agg_field": agg_field, "agg": agg,
            "columns": [], "rows": [], "total": 0, "page_count": 1,
            "error_msg": msg,  # ✅ table_search.html 쪽은 error_msg
            "table_names": [],
            "used_loose": False,
            "quota": {"code": "limit_exceeded", "kind": kind, "limit": limit, "used": used},
        }
        return render(request, "ragapp/table_search.html", ctx, status=status)

    # 마지막 안전망
    return HttpResponse(msg, status=status)


def quota_required(kind: str, *, query_param: str = "q") -> Callable:
    """
    - GET: query_param(기본 q)가 있을 때만 카운트/차단 (검색 페이지용)
    - POST: 항상 카운트/차단
    - 초과 시 429 + 화면/JSON 모두 처리
    """
    def decorator(view_func: Callable) -> Callable:
        @wraps(view_func)
        def _wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            # GET인데 q가 비어있으면(폼 진입) 카운트하지 않음
            if request.method == "GET":
                q = (request.GET.get(query_param) or "").strip()
                if not q:
                    return view_func(request, *args, **kwargs)

            try:
                allowed, limit, used = check_and_increment_usage(request, kind)
            except Exception:
                return _render_quota_block(
                    request,
                    kind=kind,
                    msg="사용량 체크 오류로 요청을 처리할 수 없습니다.",
                    status=503,
                )

            if not allowed:
                label = {"web": "웹 검색", "rag": "RAG", "pdf": "PDF", "image": "이미지 검색", "table": "표 검색"}.get(kind, kind)
                return _render_quota_block(
                    request,
                    kind=kind,
                    msg=f"오늘 사용할 수 있는 {label} 횟수를 모두 사용했습니다. 내일 다시 이용해 주세요.",
                    status=429,
                    limit=limit,
                    used=used,
                )

            return view_func(request, *args, **kwargs)

        return _wrapped
    return decorator
