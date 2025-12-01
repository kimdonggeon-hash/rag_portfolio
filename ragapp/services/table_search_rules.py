# ragapp/services/table_search_rules.py
from __future__ import annotations

from typing import Dict, List, Tuple

from django.db import OperationalError, ProgrammingError

from ragapp.models import TableSearchRule
from ragapp.table_search_rules_defaults import (
    DEFAULT_AGG_HINTS,
    DEFAULT_COLUMN_SYNONYMS,
    DEFAULT_NUMERIC_HINTS,
    DEFAULT_MIN_SIMILARITY,
    DEFAULT_HARD_FILTER_ENABLED,
)


def load_effective_rules() -> Tuple[
    Dict[str, List[str]],  # agg_hints
    Dict[str, List[str]],  # column_synonyms
    List[str],             # numeric_hints
    float,                 # min_similarity
    bool,                  # hard_filter_enabled
]:
    """
    - TableSearchRule 에 값이 있으면 그걸 우선 사용
    - 비어 있거나, DB 자체가 아직 없으면(table_search_config 미그레이션 전 등)
      table_search_rules_defaults 의 기본값 사용
    """
    # 마이그레이션 전 등에서 OperationalError / ProgrammingError 날 수 있으니 방어
    try:
        cfg = TableSearchRule.objects.order_by("-updated_at", "-id").first()
    except (OperationalError, ProgrammingError):
        cfg = None

    if cfg is None:
        return (
            DEFAULT_AGG_HINTS,
            DEFAULT_COLUMN_SYNONYMS,
            DEFAULT_NUMERIC_HINTS,
            DEFAULT_MIN_SIMILARITY,
            DEFAULT_HARD_FILTER_ENABLED,
        )

    agg_hints = cfg.agg_hints if isinstance(cfg.agg_hints, dict) else {}
    if not agg_hints:
        agg_hints = DEFAULT_AGG_HINTS

    column_synonyms = (
        cfg.column_synonyms if isinstance(cfg.column_synonyms, dict) else {}
    )
    if not column_synonyms:
        column_synonyms = DEFAULT_COLUMN_SYNONYMS

    numeric_hints = (
        cfg.numeric_hints if isinstance(cfg.numeric_hints, list) else []
    )
    if not numeric_hints:
        numeric_hints = DEFAULT_NUMERIC_HINTS

    try:
        min_sim = float(cfg.min_similarity)
    except Exception:
        min_sim = DEFAULT_MIN_SIMILARITY

    hard_filter = (
        bool(cfg.hard_filter_enabled)
        if cfg.hard_filter_enabled is not None
        else DEFAULT_HARD_FILTER_ENABLED
    )

    return agg_hints, column_synonyms, numeric_hints, min_sim, hard_filter
