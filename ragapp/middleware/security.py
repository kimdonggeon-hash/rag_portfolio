# ragapp/middleware/security.py
import re
from urllib.parse import unquote

from django.http import HttpResponseNotFound


def _normalize_path(path: str) -> str:
    p = path or "/"
    p = p.replace("\\", "/").replace("\x00", "")

    for _ in range(2):
        new = unquote(p)
        if new == p:
            break
        p = new

    # collapse repeated slashes: //healthz -> /healthz
    p = re.sub(r"/{2,}", "/", p)
    return p


SENSITIVE_PATH_RE = re.compile(
    r"(^|/)(?:"
    r"\.env(?:$|[./])|"
    r"\.git(?:$|/)|"
    r"\.svn(?:$|/)|"
    r"\.hg(?:$|/)|"
    r"\.DS_Store$|"
    r"\.htaccess$|"
    r"\.htpasswd$|"
    r"\.ssh(?:$|/)|"
    r"id_rsa$|"
    r"authorized_keys$"
    r")",
    re.IGNORECASE,
)

SCANNER_PATH_RE = re.compile(
    r"^/(?:"   # NOTE: no empty alternative!
    r"wp-admin|wp-login\.php|xmlrpc\.php|"
    r"phpmyadmin|pma|adminer\.php|"
    r"server-status|cgi-bin|"
    r"actuator|env"
    r")(?:/|$)",
    re.IGNORECASE,
)

TRAVERSAL_RE = re.compile(r"(^|/)\.\.(?:/|$)")


class BlockProbesMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Rare/legacy methods
        if request.method in {"TRACE", "TRACK", "CONNECT"}:
            return HttpResponseNotFound()

        path = _normalize_path(request.path)

        # Health check must pass
        if path in ("/healthz", "/healthz/"):
            return self.get_response(request)

        # Allow well-known
        if path.startswith("/.well-known/"):
            return self.get_response(request)

        # Block dotfile at root
        if path.startswith("/."):
            return HttpResponseNotFound()

        if SENSITIVE_PATH_RE.search(path):
            return HttpResponseNotFound()

        if SCANNER_PATH_RE.match(path):
            return HttpResponseNotFound()

        if TRAVERSAL_RE.search(path):
            return HttpResponseNotFound()

        return self.get_response(request)