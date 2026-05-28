# ragapp/ops/bootstrap_admin.py
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict

from django.contrib.auth import get_user_model


@dataclass(frozen=True)
class AdminResetResult:
    created: bool
    pk: Any
    identifier_field: str
    identifier_value: str


def create_or_reset_admin_from_env() -> AdminResetResult:
    """
    Required:
      - ADMIN_PASSWORD

    Optional:
      - ADMIN_USERNAME (default: admin)
      - ADMIN_EMAIL
    """
    User = get_user_model()

    admin_username = (os.environ.get("ADMIN_USERNAME") or "admin").strip() or "admin"
    admin_email = (os.environ.get("ADMIN_EMAIL") or "").strip()
    admin_password = (os.environ.get("ADMIN_PASSWORD") or "").strip()

    if not admin_password:
        raise RuntimeError("ADMIN_PASSWORD is empty. (Secret/env로 넣어줘)")

    username_field = getattr(User, "USERNAME_FIELD", "username") or "username"
    if username_field == "email":
        if not admin_email:
            raise RuntimeError("USERNAME_FIELD가 email인데 ADMIN_EMAIL이 비어있음.")
        identifier_value = admin_email
    else:
        identifier_value = admin_username

    lookup = {username_field: identifier_value}
    defaults: Dict[str, Any] = {}
    if admin_email and hasattr(User, "email"):
        defaults["email"] = admin_email

    u, created = User.objects.get_or_create(**lookup, defaults=defaults)

    # 업데이트
    if admin_email and hasattr(u, "email"):
        u.email = admin_email
    u.is_staff = True
    u.is_superuser = True
    u.set_password(admin_password)
    u.save()

    return AdminResetResult(
        created=created,
        pk=u.pk,
        identifier_field=username_field,
        identifier_value=identifier_value,
    )
