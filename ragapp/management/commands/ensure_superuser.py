from __future__ import annotations

import os
from typing import Any, Optional

from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.db import transaction


def _dequote(v: Optional[str]) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    return s


def _env(name: str, default: str = "") -> str:
    return _dequote(os.environ.get(name)) or default


class Command(BaseCommand):
    help = "Ensure a superuser exists; ALWAYS reset password & privileges if user already exists."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--username", default="", help="Override ADMIN_USERNAME")
        parser.add_argument("--email", default="", help="Override ADMIN_EMAIL")
        parser.add_argument("--password", default="", help="Override ADMIN_PASSWORD (avoid typing secrets on CLI)")

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        # 우선순위: CLI > ENV > default
        username = (options.get("username") or "").strip() or _env("ADMIN_USERNAME", "admin").strip()
        email = (options.get("email") or "").strip() or _env("ADMIN_EMAIL", "").strip()
        password = (options.get("password") or "").strip() or _env("ADMIN_PASSWORD", "").strip()

        if not username:
            raise RuntimeError("ensure_superuser: ADMIN_USERNAME missing/empty")
        if not password:
            # 예전처럼 skip 하면 Cloud Run에서 '로그인 안됨'만 반복되므로, 여기서 확실히 실패시키는 게 맞음
            raise RuntimeError("ensure_superuser: ADMIN_PASSWORD missing/empty")

        User = get_user_model()

        user = User.objects.filter(username=username).first()
        created = False
        if user is None:
            created = True
            user = User(username=username)

        # ✅ 항상 권한 강제
        if hasattr(user, "is_active"):
            user.is_active = True
        if hasattr(user, "is_staff"):
            user.is_staff = True
        if hasattr(user, "is_superuser"):
            user.is_superuser = True

        # 이메일은 들어오면 갱신(없으면 기존 유지)
        if email and hasattr(user, "email"):
            user.email = email

        # ✅ 항상 비밀번호 강제 갱신
        user.set_password(password)

        user.save()

        self.stdout.write(self.style.SUCCESS(
            f"ensure_superuser: {'created' if created else 'updated'} username={username}"
        ))
