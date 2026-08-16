"""Small HTTP helpers used by the project's root URL configuration.

Keeping these responses outside :mod:`ragsite.urls` lets the URL module focus
on route registration. The public behavior is intentionally unchanged.
"""

from django.contrib.staticfiles import finders
from django.http import FileResponse, HttpResponse, JsonResponse
from django.views.decorators.http import require_GET


def missing_view(name: str):
    """Return the existing 501 response for an unavailable optional view."""

    def _view(_request, *args, **kwargs):
        return JsonResponse(
            {"ok": False, "error": f"{name} view not available"},
            status=501,
            json_dumps_params={"ensure_ascii": False},
        )

    return _view


def hello(_request):
    return HttpResponse("urls.py 연결 OK")


def chrome_devtools_manifest(_request):
    return JsonResponse({}, status=200)


@require_GET
def favicon(_request):
    path = finders.find("ragapp/favicon.ico")
    if not path:
        return HttpResponse(status=204)
    return FileResponse(open(path, "rb"), content_type="image/x-icon")
