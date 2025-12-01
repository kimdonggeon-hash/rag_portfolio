from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from ragapp.models_chat_retention import LiveChatMessage, ChatEvidence, RetentionClass, PurgeRun

class Command(BaseCommand):
    help = "purge_at 기준으로 채팅/증빙 로그를 자동 파기합니다."

    def handle(self, *args, **options):
        run = PurgeRun.objects.create(status="running")
        try:
            now = timezone.now()

            msg_qs = LiveChatMessage.objects.filter(purge_at__lte=now).exclude(retention_class=RetentionClass.LEGAL_HOLD)
            msg_cnt = msg_qs.count()
            msg_qs.delete()

            ev_qs = ChatEvidence.objects.filter(purge_at__lte=now)
            ev_cnt = ev_qs.count()
            ev_qs.delete()

            run.status = "success"
            run.messages_deleted = msg_cnt
            run.evidences_deleted = ev_cnt
            run.finished_at = timezone.now()
            run.save(update_fields=["status","messages_deleted","evidences_deleted","finished_at"])

            self.stdout.write(self.style.SUCCESS(f"✅ Purged messages={msg_cnt}, evidences={ev_cnt} (now={now})"))
        except Exception as e:
            run.status = "error"
            run.note = str(e)
            run.finished_at = timezone.now()
            run.save(update_fields=["status","note","finished_at"])
            raise
