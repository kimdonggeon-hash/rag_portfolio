# ragapp/utils/db_guard.py
from __future__ import annotations

import os
import time
from typing import Dict, Tuple
from django.apps import apps

_TABLE_EXISTS_TTL = int(os.environ.get("TABLE_EXISTS_TTL", "30"))  # seconds
_table_exists_cache: Dict[str, Tuple[float, bool]] = {}


def has_table_sqlite(table_name: str) -> bool:
    """
    - 앱 초기화 중(apps.ready=False)에는 DB 접근 금지
    - 런타임에는 TTL 캐시로 DB 과다 접근 방지
    - sqlite_master 기반(지금 SQLite 쓰는 상황에 최적)
    """
    if not apps.ready:
        return False

    now = time.time()
    cached = _table_exists_cache.get(table_name)
    if cached and (now - cached[0]) < _TABLE_EXISTS_TTL:
        return cached[1]

    ok = False
    try:
        from django.db import connection
        with connection.cursor() as c:
            c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=%s",
                [table_name],
            )
            ok = (c.fetchone() is not None)
    except Exception:
        ok = False

    _table_exists_cache[table_name] = (now, ok)
    return ok
