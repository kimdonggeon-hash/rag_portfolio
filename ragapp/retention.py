# ragapp/retention.py
from __future__ import annotations

import datetime as dt
from typing import Optional, Tuple

from django.conf import settings
from django.db import models
from django.utils import timezone


_CREATED_FIELD_CANDIDATES = ("created_at", "created", "created_on", "timestamp", "created_time", "ts")


def _label_for_model(model: type[models.Model]) -> str:
    return f"{model._meta.app_label}.{model.__name__}"


def retention_days_for_model_label(model_label: str) -> int:
    by_model = getattr(settings, "DATA_RETENTION_DAYS_BY_MODEL", {}) or {}
    if model_label in by_model:
        return int(by_model[model_label])

    security_models = set(getattr(settings, "DATA_RETENTION_SECURITY_MODELS", []) or [])
    if model_label in security_models:
        return int(getattr(settings, "DATA_RETENTION_DAYS_SECURITY_DEFAULT", 365))

    return int(getattr(settings, "DATA_RETENTION_DAYS_DEFAULT", 90))


def retention_days_for_model(model: type[models.Model]) -> int:
    return retention_days_for_model_label(_label_for_model(model))


def pick_created_field(model: type[models.Model]) -> Optional[str]:
    field_names = {f.name for f in model._meta.get_fields() if getattr(f, "concrete", False)}
    for cand in _CREATED_FIELD_CANDIDATES:
        if cand in field_names:
            return cand
    return None


def compute_min_delete_at(created_at: dt.datetime, days: int) -> dt.datetime:
    if timezone.is_naive(created_at):
        created_at = timezone.make_aware(created_at, timezone.get_current_timezone())
    return created_at + dt.timedelta(days=int(days))


def coerce_delete_at_to_policy(
    *, created_at: Optional[dt.datetime], delete_at: Optional[dt.datetime], days: int
) -> Tuple[Optional[dt.datetime], bool]:
    """
    returns (new_delete_at, changed)
    - created_at이 없으면 정책 강제 불가(그대로 둠)
    - delete_at이 None이면 정책 강제 불가(그대로 둠)
    - delete_at이 너무 이르면 created_at+days로 끌어올림
    """
    if created_at is None or delete_at is None:
        return delete_at, False
    min_dt = compute_min_delete_at(created_at, days)
    if timezone.is_naive(delete_at):
        delete_at = timezone.make_aware(delete_at, timezone.get_current_timezone())
    if delete_at < min_dt:
        return min_dt, True
    return delete_at, False
