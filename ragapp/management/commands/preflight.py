from __future__ import annotations

import os
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

class Command(BaseCommand):
    help = "Production preflight checks (env/settings/static/security)."

    def _must(self, ok: bool, msg: str):
        if not ok:
            raise CommandError(msg)

    def handle(self, *args, **options):
        # 1) DEBUG
        self._must(settings.DEBUG is False, "DEBUG must be False in production.")

        # 2) SECRET_KEY
        sk = getattr(settings, "SECRET_KEY", "") or ""
        self._must(len(sk) >= 40, "SECRET_KEY looks too short/empty.")
        self._must("django-insecure" not in sk, "SECRET_KEY still contains 'django-insecure'.")

        # 3) ALLOWED_HOSTS
        ah = list(getattr(settings, "ALLOWED_HOSTS", []) or [])
        self._must(len(ah) > 0 and "*" not in ah, "ALLOWED_HOSTS must be set and not contain '*'.")

        # 4) CSRF_TRUSTED_ORIGINS (https 권장)
        cto = list(getattr(settings, "CSRF_TRUSTED_ORIGINS", []) or [])
        self._must(len(cto) > 0, "CSRF_TRUSTED_ORIGINS should include your https domain(s).")
        self._must(all(x.startswith("https://") for x in cto), "CSRF_TRUSTED_ORIGINS should be https://...")

        # 5) HTTPS / Proxy header
        sph = getattr(settings, "SECURE_PROXY_SSL_HEADER", None)
        self._must(
            sph in (("HTTP_X_FORWARDED_PROTO", "https"), ["HTTP_X_FORWARDED_PROTO", "https"]),
            "SECURE_PROXY_SSL_HEADER should be ('HTTP_X_FORWARDED_PROTO','https') for Cloud Run-like proxies."
        )

        # 6) Cookies secure
        self._must(getattr(settings, "SESSION_COOKIE_SECURE", False) is True, "SESSION_COOKIE_SECURE should be True.")
        self._must(getattr(settings, "CSRF_COOKIE_SECURE", False) is True, "CSRF_COOKIE_SECURE should be True.")

        # 7) Static
        static_root = getattr(settings, "STATIC_ROOT", None)
        self._must(bool(static_root), "STATIC_ROOT must be set (for collectstatic).")

        self.stdout.write(self.style.SUCCESS("✅ Preflight OK: production settings look sane."))
