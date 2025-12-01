# ragapp/management/commands/purge_old_data.py
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import DateTimeField
from django.utils import timezone


def _bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s not in ("0", "false", "no", "off", "")


def _pick_dt_field(model) -> Optional[str]:
    """
    모델에서 '시간 기준' 필드를 자동으로 고른다.
    우선순위:
      1) delete_at (예약 삭제가 있는 모델)
      2) created_at
      3) ended_at / started_at
      4) updated_at
      5) 그 외 DateTimeField 아무거나
    """
    field_names = {f.name for f in model._meta.get_fields() if hasattr(f, "name")}
    for cand in ("delete_at", "created_at", "ended_at", "started_at", "updated_at"):
        if cand in field_names:
            return cand

    # fallback: DateTimeField 중 아무거나
    for f in model._meta.get_fields():
        if isinstance(f, DateTimeField):
            return f.name
    return None


class Command(BaseCommand):
    help = "보존기간이 지난 데이터(로그/피드백 등)를 안전하게 정리합니다. (RagChunk 등은 대상에서 제외하세요)"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="실제로 삭제합니다. (기본은 dry-run)")
        parser.add_argument("--model", action="append", default=[], help="특정 모델만 실행 (여러 번 지정 가능)")
        parser.add_argument("--days", type=int, default=None, help="전체 모델에 공통으로 적용할 보존기간(일) override")
        parser.add_argument("--verbose", action="store_true", help="상세 출력")

    def handle(self, *args, **opts):
        apply = bool(opts.get("apply"))
        only_models: List[str] = [str(x) for x in (opts.get("model") or []) if str(x).strip()]
        override_days = opts.get("days")
        verbose = bool(opts.get("verbose"))

        enabled = _bool(getattr(settings, "AUTO_PURGE_ENABLED", False), default=False)
        if not enabled:
            self.stdout.write(self.style.WARNING("AUTO_PURGE_ENABLED=0 (비활성). settings.py에서 켜고 실행하세요."))
            return

        allowlist: List[str] = list(getattr(settings, "AUTO_PURGE_ALLOWLIST", []) or [])
        per_model_days: Dict[str, int] = dict(getattr(settings, "AUTO_PURGE_RETENTION_DAYS", {}) or {})
        never: List[str] = list(getattr(settings, "AUTO_PURGE_NEVER", []) or [])

        if only_models:
            allowlist = [m for m in allowlist if m in set(only_models)]

        # 안전장치: never는 무조건 제외
        allowlist = [m for m in allowlist if m not in set(never)]

        if not allowlist:
            self.stdout.write(self.style.WARNING("삭제 대상이 비어있습니다. (AUTO_PURGE_ALLOWLIST 확인)"))
            return

        now = timezone.now()
        total_deleted = 0
        total_candidates = 0
        results: List[Tuple[str, str, int, int]] = []  # (model, field, days, deleted_or_candidates)

        for model_name in allowlist:
            model = apps.get_model("ragapp", model_name)
            if model is None:
                self.stdout.write(self.style.WARNING(f"- {model_name}: 모델을 찾을 수 없음 (skipped)"))
                continue

            dt_field = _pick_dt_field(model)
            if not dt_field:
                self.stdout.write(self.style.WARNING(f"- {model_name}: DateTime 기준 필드가 없어 스킵"))
                continue

            days = int(override_days) if override_days is not None else int(per_model_days.get(model_name, 30))
            if days <= 0:
                self.stdout.write(self.style.WARNING(f"- {model_name}: days={days} (<=0) 스킵"))
                continue

            cutoff = now - timedelta(days=days)
            qs = model.objects.all()

            # 안전장치: legal_hold 같은 필드가 있으면 hold는 제외
            field_names = {f.name for f in model._meta.get_fields() if hasattr(f, "name")}
            if "legal_hold" in field_names:
                qs = qs.exclude(legal_hold=True)

            qs = qs.filter(**{f"{dt_field}__lt": cutoff})
            cnt = qs.count()
            total_candidates += cnt

            if verbose:
                self.stdout.write(f"- {model_name}: field={dt_field}, days={days}, candidates={cnt}")

            if apply and cnt:
                deleted, _ = qs.delete()
                total_deleted += int(deleted or 0)
                results.append((model_name, dt_field, days, int(deleted or 0)))
            else:
                results.append((model_name, dt_field, days, cnt))

        if apply:
            self.stdout.write(self.style.SUCCESS(f"완료: deleted={total_deleted} (candidates={total_candidates})"))
        else:
            self.stdout.write(self.style.SUCCESS(f"DRY-RUN 완료: candidates={total_candidates} (삭제는 안 함)"))

        if verbose:
            for model_name, field, days, n in results:
                label = "deleted" if apply else "candidates"
                self.stdout.write(f"  · {model_name} ({field}, {days}d): {label}={n}")
