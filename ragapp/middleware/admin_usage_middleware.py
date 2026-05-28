# ragapp/middleware/admin_usage_middleware.py
from __future__ import annotations

from django.utils.deprecation import MiddlewareMixin

from ragapp.services.usage_limiter import bump_admin_usage


class AdminUsageMiddleware(MiddlewareMixin):
    """
    ✅ /admin/, /ragadmin/ 접근 시 staff에게 admin_count +1
    """

    ADMIN_PREFIXES = ("/admin/", "/ragadmin/")

    def process_request(self, request):
        path = getattr(request, "path", "") or ""
        if path.startswith(self.ADMIN_PREFIXES):
            bump_admin_usage(request)
        return None
