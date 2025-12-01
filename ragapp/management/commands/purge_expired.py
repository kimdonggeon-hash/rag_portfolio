from __future__ import annotations

from datetime import timedelta

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone


def _get_days(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default) or default)
    except Exception:
        return default


class Command(BaseCommand):
    help = "delete_at(또는 created_at) 기준으로 보존기간 지난 레코드를 배치 삭제합니다."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="삭제하지 않고 대상 개수만 출력")
        parser.add_argument("--batch", type=int, default=2000, help="한 번에 지울 최대 개수(모델별)")
        parser.add_argument("--noinput", action="store_true", help="확인 없이 실행")

    def handle(self, *args, **opts):
        dry = bool(opts["dry_run"])
        batch = int(opts["batch"] or 2000)
        noinput = bool(opts["noinput"])

        now = timezone.now()

        # ✅ 여기서 “지울 대상”만 명확히 allowlist로 관리 (안전)
        targets = [
            # (app_label, model_name, mode, retention_setting_name, default_days)

            # 개인정보 가능성 높은 로그 → 짧게
            ("ragapp", "ChatQueryLog", "delete_at", "RETENTION_DAYS_CHATLOG", 30),
            ("ragapp", "Feedback", "delete_at", "RETENTION_DAYS_FEEDBACK", 180),
            ("ragapp", "QaragFeedback", "delete_at", "RETENTION_DAYS_QARAG_FEEDBACK", 180),
            ("ragapp", "FeedbackLog", "created_at", "RETENTION_DAYS_FEEDBACKLOG", 180),

            # 운영 로그
            ("ragapp", "AppLog", "created_at", "RETENTION_DAYS_APPLOG", 30),
            ("ragapp", "MyLog", "created_at", "RETENTION_DAYS_MYLOG", 180),
            ("ragapp", "IngestHistory", "created_at", "RETENTION_DAYS_INGESTHISTORY", 365),

            # 증빙/권리행사
            ("ragapp", "ConsentLog", "delete_at", "RETENTION_DAYS_CONSENT", 365),
            ("ragapp", "AuditEvent", "created_at", "RETENTION_DAYS_AUDIT", 1095),
            ("ragapp", "DataErasureTicket", "created_at", "RETENTION_DAYS_DSR", 1095),

            # 라이브챗(원하면)
            ("ragapp", "LiveChatSession", "created_at", "RETENTION_DAYS_LIVECHAT", 180),
            ("ragapp", "LiveChatRoom", "created_at", "RETENTION_DAYS_LIVECHATROOM", 90),
        ]

        total_candidates = 0
        plan = []

        for app_label, model_name, mode, setting_name, default_days in targets:
            Model = apps.get_model(app_label, model_name)
            days = _get_days(setting_name, default_days)

            if days <= 0:
                continue

            if mode == "delete_at":
                qs = Model.objects.all()
                field_names = {f.name for f in Model._meta.get_fields() if hasattr(f, "name")}
                if "legal_hold" in field_names:
                    qs = qs.filter(legal_hold=False)
                qs = qs.filter(delete_at__isnull=False, delete_at__lt=now)
            else:
                cutoff = now - timedelta(days=days)
                qs = Model.objects.filter(created_at__lt=cutoff)

            cnt = qs.count()
            if cnt:
                plan.append((Model, qs, cnt, days, mode))
                total_candidates += cnt

        if not plan:
            self.stdout.write(self.style.SUCCESS("삭제 대상 없음"))
            return

        self.stdout.write("\n[삭제 계획]")
        for Model, _qs, cnt, days, mode in plan:
            self.stdout.write(f"- {Model._meta.label}: {cnt}건 (mode={mode}, days={days})")

        self.stdout.write(f"\n총 {total_candidates}건 대상")
        if dry:
            self.stdout.write(self.style.WARNING("dry-run 이라 삭제는 수행하지 않았습니다."))
            return

        if not noinput:
            ans = input("정말 삭제할까요? (yes 입력) > ").strip().lower()
            if ans != "yes":
                self.stdout.write(self.style.WARNING("취소됨"))
                return

        deleted_total = 0
        for Model, qs, cnt, days, mode in plan:
            # 큰 delete는 쪼개서 안전하게
            while True:
                pks = list(qs.values_list("pk", flat=True)[:batch])
                if not pks:
                    break
                Model.objects.filter(pk__in=pks).delete()
                deleted_total += len(pks)

        self.stdout.write(self.style.SUCCESS(f"삭제 완료: {deleted_total}건"))
