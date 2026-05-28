# ragapp/management/commands/adminreset_pg.py
from __future__ import annotations

import os
from django.core.management.base import BaseCommand, CommandError
from django.core.management import call_command

from ragapp.ops.bootstrap_admin import create_or_reset_admin_from_env


class Command(BaseCommand):
    help = "Create/reset admin using ADMIN_* env vars. (Safe: does not touch Chroma indexes.)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--checks",
            action="store_true",
            help="Run `manage.py check` before admin reset.",
        )
        parser.add_argument(
            "--migrate",
            action="store_true",
            help="Run `manage.py migrate --noinput` before admin reset.",
        )
        parser.add_argument(
            "--collectstatic",
            action="store_true",
            help="Run `manage.py collectstatic --noinput` before admin reset.",
        )
        parser.add_argument(
            "--show-env",
            action="store_true",
            help="Print key env vars snapshot for debugging (DB/CHROMA).",
        )
        parser.add_argument(
            "--strict-chroma-images",
            action="store_true",
            help="Fail if CHROMA_IMAGES_COLLECTION is set and not 'media_images'.",
        )

    def _print_env_snapshot(self):
        keys = [
            "DJANGO_SETTINGS_MODULE",
            "DATABASE_URL",
            "CLOUD_SQL_CONNECTION_NAME",
            "CHROMA_MEDIA_DIR",
            "CHROMA_IMAGES_COLLECTION",
            "PUBLIC_BASE_URL",
            "ALLOWED_HOSTS",
            "ADMIN_USERNAME",
            "ADMIN_EMAIL",
        ]
        self.stdout.write("=== [adminreset_pg] ENV snapshot ===")
        for k in keys:
            v = os.getenv(k)
            if v is None:
                self.stdout.write(f"- {k}=<unset>")
            else:
                vv = v if len(v) <= 220 else (v[:220] + "...(truncated)")
                self.stdout.write(f"- {k}={vv}")

    def handle(self, *args, **options):
        # ✅ 기본값(미설정 시에만): 운영 컬렉션 기대치와 맞춰두기
        # (이 커맨드는 Chroma를 건드리지 않지만, Job env 디버깅에 도움)
        if not (os.getenv("CHROMA_IMAGES_COLLECTION") or "").strip():
            os.environ["CHROMA_IMAGES_COLLECTION"] = "media_images"

        if options.get("show_env"):
            self._print_env_snapshot()

        if options.get("strict_chroma_images"):
            v = (os.getenv("CHROMA_IMAGES_COLLECTION") or "").strip()
            if v and v != "media_images":
                raise CommandError(
                    f"CHROMA_IMAGES_COLLECTION must be 'media_images' (got '{v}'). "
                    "Update the Cloud Run Job env vars."
                )

        if options.get("checks"):
            call_command("check")

        if options.get("migrate"):
            call_command("migrate", interactive=False)

        if options.get("collectstatic"):
            call_command("collectstatic", interactive=False, verbosity=1)

        r = create_or_reset_admin_from_env()
        self.stdout.write(
            self.style.SUCCESS(
                f"admin ok: {r.identifier_field}={r.identifier_value} pk={r.pk} created={r.created}"
            )
        )
