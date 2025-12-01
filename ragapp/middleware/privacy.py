# ragapp/middleware/privacy.py
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Dict, Tuple

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.deprecation import MiddlewareMixin

log = logging.getLogger(__name__)

_PURGE_CACHE_KEY = "privacy:retention:last_purge_at"
_PURGE_CACHE_TTL_SEC = 23 * 60 * 60  # 하루 1회 트리거(여유 있게 23h)

class PrivacyComplianceMiddleware(MiddlewareMixin):
    """
    - RETENTION_DAYS > 0 이면 하루 1회, 보관기간 지난 로그/히스토리를 자동 삭제
    - 삭제는 트래픽이 있을 때 첫 요청이 트리거
    - 실패해도 요청 흐름에는 영향 없음(로그만 남김)
    """

    def process_request(self, request):
        try:
            self._maybe_run_retention_purge()
        except Exception:  # 절대 요청을 막지 않음
            log.exception("[retention] purge hook 실패")
        return None

    # ──────────────────────────────────────────────────────────────────────
    # Retention
    # ──────────────────────────────────────────────────────────────────────
    def _maybe_run_retention_purge(self) -> None:
        days = int(getattr(settings, "RETENTION_DAYS", 0) or 0)
        if days <= 0:
            return

        now = timezone.now()
        last = cache.get(_PURGE_CACHE_KEY)
        if last and (now - last).total_seconds() < 24 * 60 * 60:
            return  # 오늘 이미 수행

        cutoff = now - timedelta(days=days)
        stats = _purge_models_older_than(cutoff)

        cache.set(_PURGE_CACHE_KEY, now, timeout=_PURGE_CACHE_TTL_SEC)
        # 요약 로그
        pretty = ", ".join(f"{k}:{v}" for k, v in stats.items())
        log.info("[retention] cutoff=%s → deleted {%s}", cutoff.isoformat(), pretty)


def _purge_models_older_than(cutoff) -> Dict[str, int]:
    """
    각 모델에서 created_at < cutoff 인 레코드 삭제.
    모델이 없거나 필드가 없으면 건너뜀(안전).
    """
    total: Dict[str, int] = {}

    # (모델, created_at 필드명)
    candidates: Tuple[Tuple[str, str], ...] = (
        ("ragapp.models.ChatQueryLog", "created_at"),
        ("ragapp.models.MyLog", "created_at"),
        ("ragapp.models.Feedback", "created_at"),
        ("ragapp.models.IngestHistory", "created_at"),
        # 필요 시 여기에 추가: ("ragapp.models.YourModel", "created_at"),
    )

    for dotted, created_field in candidates:
        try:
            model = _import_model(dotted)
            if not model:
                continue

            # created_at 없는 모델은 스킵
            if created_field not in {f.name for f in model._meta.get_fields()}:
                log.debug("[retention] %s: '%s' 필드 없음 → skip", dotted, created_field)
                continue

            qs = model.objects.filter(**{f"{created_field}__lt": cutoff})
            deleted, _ = qs.delete()
            total[model.__name__] = int(deleted)
        except Exception:
            log.exception("[retention] %s purge 실패", dotted)
    return total


def _import_model(dotted: str):
    try:
        module_name, cls_name = dotted.rsplit(".", 1)
        mod = __import__(module_name, fromlist=[cls_name])
        return getattr(mod, cls_name, None)
    except Exception:
        return None
