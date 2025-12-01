# ragapp/management/commands/purge_old_records.py
from __future__ import annotations

from datetime import timedelta
from typing import Dict

from django.core.management.base import BaseCommand
from django.conf import settings
from django.utils import timezone

from ragapp.middleware.privacy import _purge_models_older_than  # 재사용

class Command(BaseCommand):
    help = "RETENTION_DAYS에 따라 보관기간 지난 레코드를 즉시 삭제합니다."

    def handle(self, *args, **options):
        days = int(getattr(settings, "RETENTION_DAYS", 0) or 0)
        if days <= 0:
            self.stdout.write(self.style.WARNING("RETENTION_DAYS가 0이거나 미설정입니다. 작업 종료."))
            return

        cutoff = timezone.now() - timedelta(days=days)
        stats: Dict[str, int] = _purge_models_older_than(cutoff)

        self.stdout.write(self.style.SUCCESS(f"cutoff={cutoff.isoformat()}"))
        for name, n in stats.items():
            self.stdout.write(f"  - {name}: deleted {n}")
        self.stdout.write(self.style.SUCCESS("완료."))
