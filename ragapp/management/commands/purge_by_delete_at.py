# ragapp/management/commands/purge_by_delete_at.py
from __future__ import annotations

import datetime as dt
from typing import List, Optional, Tuple

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import DateTimeField, ExpressionWrapper, F
from django.utils import timezone
from django.conf import settings

from ragapp.retention import retention_days_for_model_label, pick_created_field


def _get_model(label: str):
    # label 예: "ragapp.ChatQueryLog"
    app_label, model_name = label.split(".", 1)
    return apps.get_model(app_label, model_name)


def _parse_models_arg(models_arg: str) -> List[str]:
    raw = (models_arg or "").strip()
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def _default_models_from_settings() -> List[str]:
    return list(getattr(settings, "PURGE_DELETE_AT_MODELS", []) or [])


def _has_field(model, name: str) -> bool:
    try:
        model._meta.get_field(name)
        return True
    except Exception:
        return False


class Command(BaseCommand):
    help = "delete_at(파기 예정 시각) 기준으로 만료된 데이터들을 정기 삭제합니다. (정책 최소보유기간 교정 옵션 포함)"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="삭제/수정은 하지 않고 대상 건수만 출력")
        parser.add_argument("--limit", type=int, default=5000, help="모델별 1회 실행에서 삭제할 최대 행 수")

        parser.add_argument(
            "--models",
            type=str,
            default="",
            help="특정 모델만 실행 (콤마 구분, 예: ragapp.ChatQueryLog,ragapp.AppLog). 비우면 settings.PURGE_DELETE_AT_MODELS 사용",
        )

        parser.add_argument(
            "--repair-early",
            action="store_true",
            help="delete_at이 정책(생성시각+보유기간)보다 빠른 레코드를 먼저 끌어올려 교정합니다.",
        )

        parser.add_argument(
            "--force-without-created-field",
            action="store_true",
            help="created_at 등 생성시각 필드가 없는 모델도 delete_at<=now면 삭제 허용(권장 X).",
        )

    def handle(self, *args, **opts):
        dry_run: bool = bool(opts["dry_run"])
        limit: int = int(opts["limit"])
        repair_early: bool = bool(opts["repair_early"])
        force_without_created: bool = bool(opts["force_without_created_field"])

        model_labels = _parse_models_arg(opts.get("models") or "")
        if not model_labels:
            model_labels = _default_models_from_settings()

        if not model_labels:
            self.stdout.write(self.style.WARNING("실행할 모델이 없습니다. settings.PURGE_DELETE_AT_MODELS 또는 --models를 지정하세요."))
            return

        now = timezone.now()
        grand_repaired = 0
        grand_deleted = 0

        for label in model_labels:
            model = _get_model(label)

            if not _has_field(model, "delete_at"):
                self.stdout.write(self.style.WARNING(f"[SKIP] {label}: delete_at 필드가 없어 건너뜀"))
                continue

            created_field = pick_created_field(model)
            retention_days = retention_days_for_model_label(label)

            qs = model._default_manager.all()

            # ── 1) 교정: delete_at이 너무 이르면 created+retention으로 끌어올림
            repaired = 0
            if repair_early:
                if created_field is None:
                    self.stdout.write(self.style.WARNING(f"[WARN] {label}: 생성시각 필드가 없어 repair-early 불가"))
                else:
                    min_expr = ExpressionWrapper(
                        F(created_field) + dt.timedelta(days=retention_days),
                        output_field=DateTimeField(),
                    )
                    early_qs = qs.filter(delete_at__isnull=False).filter(delete_at__lt=min_expr)
                    early_cnt = early_qs.count()

                    if early_cnt and not dry_run:
                        # update는 limit 없이 한 번에 처리(정책 교정이 목적)
                        repaired = early_qs.update(delete_at=min_expr)
                    else:
                        repaired = early_cnt

            # ── 2) 삭제: delete_at <= now 이면서 정책 최소보유기간 만족하는 것만
            due_qs = qs.filter(delete_at__isnull=False, delete_at__lte=now)

            if created_field is None:
                if not force_without_created:
                    self.stdout.write(self.style.WARNING(
                        f"[SKIP] {label}: 생성시각 필드가 없어 안전하게 삭제를 막음. "
                        f"원하면 --force-without-created-field 사용"
                    ))
                    self.stdout.write(self.style.NOTICE(f"        (repair-early로도 교정 불가. 모델에 created_at 같은 필드 권장)"))
                    continue
            else:
                min_expr = ExpressionWrapper(
                    F(created_field) + dt.timedelta(days=retention_days),
                    output_field=DateTimeField(),
                )
                due_qs = due_qs.filter(delete_at__gte=min_expr)

            # limit만큼만 지우기(대용량 안전)
            pks = list(due_qs.order_by("pk").values_list("pk", flat=True)[:limit])
            to_delete_cnt = len(pks)

            deleted = 0
            if to_delete_cnt and not dry_run:
                with transaction.atomic():
                    deleted, _ = model._default_manager.filter(pk__in=pks).delete()
                    # delete()는 cascade까지 포함한 총 삭제 수를 반환할 수 있음
            else:
                deleted = to_delete_cnt

            grand_repaired += int(repaired)
            grand_deleted += int(deleted)

            self.stdout.write(
                f"[OK] {label} | retention_days={retention_days} | repaired={repaired} | deleted={deleted} | dry_run={dry_run}"
            )

        self.stdout.write(self.style.SUCCESS(f"DONE | repaired_total={grand_repaired} | deleted_total={grand_deleted} | now={now.isoformat()}"))
