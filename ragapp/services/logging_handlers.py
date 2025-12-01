# ragapp/services/logging_handlers.py
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from django.apps import apps


class DBLogHandler(logging.Handler):
    """
    Python logging 기록을 AppLog 테이블에 적재하는 핸들러.
    - logger 이름이나 extra={"applog_extra": {...}} 를 이용해
      AppLog(component, trace_id, meta)에 매핑한다.
    """

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("django.db"):
            return

        try:
            AppLog = apps.get_model("ragapp", "AppLog")  # type: ignore
        except Exception:
            return

        # extra로 들어온 JSON
        extra = getattr(record, "applog_extra", None)
        if not isinstance(extra, dict):
            extra = {}

        # component / trace_id / meta 분리
        component = extra.get("component") or record.name
        trace_id = extra.get("trace_id", "")

        meta = extra.copy()
        meta.pop("component", None)
        meta.pop("trace_id", None)

        # 메시지 포맷팅
        try:
            msg = self.format(record)
        except Exception:
            try:
                msg = record.getMessage()
            except Exception:
                msg = str(record)

        try:
            AppLog.objects.create(
                level=record.levelname,
                component=component,
                trace_id=trace_id,
                message=msg,
                meta=meta,
            )
        except Exception:
            # 로깅 때문에 서비스가 죽으면 안 되므로 조용히 무시
            return
