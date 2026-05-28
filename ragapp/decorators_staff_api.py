from __future__ import annotations

from functools import wraps
from django.http import JsonResponse, HttpRequest

def staff_api_required(view_func):
    """
    - staff 아니면 로그인 리다이렉트(302) 대신 JSON 403을 내려준다.
    - API에 붙이기 좋음.
    """
    @wraps(view_func)
    def _wrapped(request: HttpRequest, *args, **kwargs):
        u = getattr(request, "user", None)
        if not (u and getattr(u, "is_authenticated", False) and getattr(u, "is_staff", False)):
            return JsonResponse(
                {"ok": False, "status": "error", "error": "staff_only", "code": "STAFF_ONLY"},
                status=403,
                json_dumps_params={"ensure_ascii": False},
            )
        return view_func(request, *args, **kwargs)
    return _wrapped
