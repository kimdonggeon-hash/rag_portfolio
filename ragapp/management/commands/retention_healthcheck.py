# ragapp/management/commands/retention_healthcheck.py
from __future__ import annotations

import datetime as dt
import json
from typing import Dict, List, Tuple, Optional, Any

from django.core.management.base import BaseCommand
from django.db.models import DateTimeField, ExpressionWrapper, F, Min, Max
from django.utils import timezone

from ragapp.models import (
    ConsentLog,
    ChatQueryLog,
    QaragFeedback,
    Feedback,
    _retention_days,
)


def _has_field(Model, field_name: str) -> bool:
    try:
        Model._meta.get_field(field_name)
        return True
    except Exception:
        return False


def _parse_models_arg(raw: str) -> List[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


class Command(BaseCommand):
    help = "Retention / delete_at 상태 점검(읽기 전용): backfill 대상, purge 대상, early delete_at, legal_hold 등을 요약합니다."

    def add_arguments(self, parser):
        parser.add_argument("--models", type=str, default="", help="ConsentLog,ChatQueryLog,QaragFeedback,Feedback (비우면 전부)")
        parser.add_argument("--json", action="store_true", help="JSON으로 출력(로그/잡에서 파싱용)")
        parser.add_argument("--skip-total", action="store_true", help="총 레코드 수(count) 계산 생략(아주 큰 테이블일 때)")

    def handle(self, *args, **opts):
        selected = set(_parse_models_arg(opts.get("models") or ""))
        as_json: bool = bool(opts["json"])
        skip_total: bool = bool(opts["skip_total"])

        # 모델 + retention key 매핑(네 backfill 로직과 동일하게)
        days_q = _retention_days("RETENTION_DAYS_QARAG_FEEDBACK", 0) or _retention_days("RETENTION_DAYS_FEEDBACK", 0)

        targets: List[Tuple[str, Any, int]] = [
            ("ConsentLog", ConsentLog, _retention_days("RETENTION_DAYS_CONSENT", 0)),
            ("ChatQueryLog", ChatQueryLog, _retention_days("RETENTION_DAYS_CHATLOG", 0)),
            ("QaragFeedback", QaragFeedback, days_q),
            ("Feedback", Feedback, _retention_days("RETENTION_DAYS_FEEDBACK", 0)),
        ]

        now = timezone.now()

        rows: List[Dict[str, Any]] = []

        for name, Model, days in targets:
            if selected and name not in selected:
                continue

            row: Dict[str, Any] = {
                "model": name,
                "retention_days": int(days or 0),
                "has_delete_at": _has_field(Model, "delete_at"),
                "has_created_at": _has_field(Model, "created_at"),
                "has_legal_hold": _has_field(Model, "legal_hold"),
            }

            qs = Model.objects.all()

            # total (옵션)
            if not skip_total:
                row["total"] = qs.count()
            else:
                row["total"] = None

            # legal_hold
            if row["has_legal_hold"]:
                row["legal_hold_true"] = qs.filter(legal_hold=True).count()
                qs_active = qs.filter(legal_hold=False)
            else:
                row["legal_hold_true"] = None
                qs_active = qs

            # delete_at NULL (backfill 후보)
            if row["has_delete_at"]:
                row["delete_at_null_active"] = qs_active.filter(delete_at__isnull=True).count()
                row["delete_at_null_all"] = qs.filter(delete_at__isnull=True).count()
            else:
                row["delete_at_null_active"] = None
                row["delete_at_null_all"] = None

            # created_at NULL (이상치)
            if row["has_created_at"]:
                row["created_at_null_active"] = qs_active.filter(created_at__isnull=True).count()
                row["created_at_null_all"] = qs.filter(created_at__isnull=True).count()
            else:
                row["created_at_null_active"] = None
                row["created_at_null_all"] = None

            # purge 후보: delete_at <= now (legal_hold 제외)
            if row["has_delete_at"]:
                row["purge_due_active"] = qs_active.filter(delete_at__isnull=False, delete_at__lte=now).count()
            else:
                row["purge_due_active"] = None

            # early delete_at: delete_at < created_at + retention_days (legal_hold 제외)
            # (retention_days=0이면 의미가 없어서 skip)
            if row["has_delete_at"] and row["has_created_at"] and (days or 0) > 0:
                min_expr = ExpressionWrapper(
                    F("created_at") + dt.timedelta(days=int(days)),
                    output_field=DateTimeField(),
                )
                row["delete_at_early_active"] = qs_active.filter(
                    delete_at__isnull=False,
                    created_at__isnull=False,
                    delete_at__lt=min_expr,
                ).count()
            else:
                row["delete_at_early_active"] = None

            # delete_at min/max 범위 (참고용)
            if row["has_delete_at"]:
                agg = qs_active.aggregate(
                    min_delete_at=Min("delete_at"),
                    max_delete_at=Max("delete_at"),
                )
                row["min_delete_at_active"] = agg["min_delete_at"].isoformat() if agg["min_delete_at"] else None
                row["max_delete_at_active"] = agg["max_delete_at"].isoformat() if agg["max_delete_at"] else None
            else:
                row["min_delete_at_active"] = None
                row["max_delete_at_active"] = None

            rows.append(row)

        payload = {
            "now": now.isoformat(),
            "models": rows,
        }

        if as_json:
            self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        # 텍스트 출력(사람이 보기 좋게)
        self.stdout.write(f"Retention healthcheck | now={payload['now']}")
        for r in rows:
            self.stdout.write(
                f"- {r['model']} | days={r['retention_days']}"
                f" | total={r['total'] if r['total'] is not None else 'skip'}"
                f" | legal_hold_true={r['legal_hold_true'] if r['legal_hold_true'] is not None else 'n/a'}"
                f" | delete_at_null(active)={r['delete_at_null_active'] if r['delete_at_null_active'] is not None else 'n/a'}"
                f" | purge_due(active)={r['purge_due_active'] if r['purge_due_active'] is not None else 'n/a'}"
                f" | early(active)={r['delete_at_early_active'] if r['delete_at_early_active'] is not None else 'n/a'}"
                f" | delete_at[min,max]=({r['min_delete_at_active']},{r['max_delete_at_active']})"
            )
