# ragapp/management/commands/purge_expired_trash.py
from __future__ import annotations

from django.core.management.base import BaseCommand

from ragapp.services.trash_service import purge_expired_trash


class Command(BaseCommand):
    help = (
        "휴지통(TrashedRecord)에서 보관 기간이 지난 항목을 자동으로 영구 삭제합니다. "
        "기간은 기본적으로 /ragadmin/trash/ 화면에서 관리자가 고른 TrashSettings 값을 따릅니다."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=None,
            help="이 값을 주면 TrashSettings 대신 이 일수를 기준으로 삭제합니다.",
        )

    def handle(self, *args, **opts):
        days = opts.get("days")
        count = purge_expired_trash(retention_days=days)
        self.stdout.write(f"[purge_expired_trash] 영구 삭제된 항목: {count}개")
