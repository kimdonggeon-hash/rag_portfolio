# ragapp/management/commands/backfill_delete_at.py
from __future__ import annotations

from typing import Iterable, List, Optional

from django.core.management.base import BaseCommand
from django.db import transaction

from ragapp.models import (
    ConsentLog,
    ChatQueryLog,
    QaragFeedback,
    Feedback,
    _retention_days,
    _compute_delete_at,
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
    help = "기존 레코드 중 delete_at이 비어있는 것들을 created_at + retention_days로 채웁니다(legal_hold 제외)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="실제 업데이트 없이 대상 건수만 출력")
        parser.add_argument("--limit", type=int, default=0, help="모델별 최대 처리 건수(0이면 제한 없음)")
        parser.add_argument("--chunk-size", type=int, default=500, help="iterator/bulk_update 배치 크기(기본 500)")
        parser.add_argument(
            "--models",
            type=str,
            default="",
            help="실행할 모델 선택(콤마 구분): ConsentLog,ChatQueryLog,QaragFeedback,Feedback (비우면 전부)",
        )

    def handle(self, *args, **options):
        dry_run: bool = bool(options["dry_run"])
        limit: int = int(options["limit"] or 0)
        chunk_size: int = int(options["chunk_size"] or 500)
        selected = set(_parse_models_arg(options.get("models") or ""))

        self.stdout.write("Backfill delete_at started...")

        def _run_one(Model, days: int, name: str) -> int:
            if selected and name not in selected:
                self.stdout.write(f"- {name}: not selected -> skip")
                return 0

            if not days or days <= 0:
                self.stdout.write(f"- {name}: days=0 -> skip")
                return 0

            if not _has_field(Model, "delete_at"):
                self.stdout.write(f"- {name}: no delete_at field -> skip")
                return 0

            if not _has_field(Model, "created_at"):
                self.stdout.write(f"- {name}: no created_at field -> skip")
                return 0

            qs = Model.objects.filter(delete_at__isnull=True).order_by("id")

            # legal_hold 필드가 있으면 보류 레코드는 제외(안전)
            if _has_field(Model, "legal_hold"):
                qs = qs.filter(legal_hold=False)

            # created_at NULL은 계산 불가하니 제외(안전)
            qs = qs.filter(created_at__isnull=False)

            if limit > 0:
                qs = qs[:limit]

            if dry_run:
                cnt = qs.count()
                self.stdout.write(f"- {name}: would fill {cnt} (dry_run=True)")
                return int(cnt)

            updated = 0
            batch = []

            # 대량 대비: iterator + bulk_update로 쿼리 폭발 방지
            for obj in qs.iterator(chunk_size=chunk_size):
                obj.delete_at = _compute_delete_at(obj.created_at, days)
                batch.append(obj)

                if len(batch) >= chunk_size:
                    Model.objects.bulk_update(batch, ["delete_at"], batch_size=chunk_size)
                    updated += len(batch)
                    batch.clear()

            if batch:
                Model.objects.bulk_update(batch, ["delete_at"], batch_size=chunk_size)
                updated += len(batch)

            self.stdout.write(f"- {name}: filled {updated}")
            return int(updated)

        total = 0

        # 트랜잭션을 “전체 한 방”으로 길게 잡기보다, 모델별로 짧게(실무 안정성)
        with transaction.atomic():
            total += _run_one(ConsentLog, _retention_days("RETENTION_DAYS_CONSENT", 0), "ConsentLog")

        with transaction.atomic():
            total += _run_one(ChatQueryLog, _retention_days("RETENTION_DAYS_CHATLOG", 0), "ChatQueryLog")

        days_q = _retention_days("RETENTION_DAYS_QARAG_FEEDBACK", 0) or _retention_days("RETENTION_DAYS_FEEDBACK", 0)
        with transaction.atomic():
            total += _run_one(QaragFeedback, days_q, "QaragFeedback")

        with transaction.atomic():
            total += _run_one(Feedback, _retention_days("RETENTION_DAYS_FEEDBACK", 0), "Feedback")

        self.stdout.write(f"Backfill delete_at done. total_filled={total} dry_run={dry_run}")
