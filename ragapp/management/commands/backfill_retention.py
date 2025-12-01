from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from ragapp.models_chat_retention import LiveChatMessage, ChatEvidence


class Command(BaseCommand):
    help = "purge_at/보관정책 필드를 백필합니다(purge_at 비어있는 기존 데이터 대상)."

    def handle(self, *args, **options):
        with transaction.atomic():
            m_qs = LiveChatMessage.objects.filter(purge_at__isnull=True).order_by("id")
            m_cnt = 0
            for m in m_qs.iterator(chunk_size=500):
                m.apply_retention(force=True)
                m.save(update_fields=["purge_at"])
                m_cnt += 1

            e_qs = ChatEvidence.objects.filter(purge_at__isnull=True).order_by("id")
            e_cnt = 0
            for e in e_qs.iterator(chunk_size=500):
                e.save(update_fields=["purge_at"])
                e_cnt += 1

        self.stdout.write(self.style.SUCCESS(f"✅ Backfilled messages={m_cnt}, evidences={e_cnt}"))
