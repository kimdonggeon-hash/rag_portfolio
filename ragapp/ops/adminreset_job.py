# ragapp/ops/adminreset_job.py
from __future__ import annotations

import os
import sys
from pathlib import Path

import django


def main() -> int:
    # manage.py 있는 프로젝트 루트를 sys.path에 추가
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))

    # ✅ 네 프로젝트는 ragsite.settings
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ragsite.settings")

    django.setup()

    from ragapp.ops.bootstrap_admin import create_or_reset_admin_from_env

    r = create_or_reset_admin_from_env()
    print(f"admin ok: {r.identifier_field}={r.identifier_value} pk={r.pk} created={r.created}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
