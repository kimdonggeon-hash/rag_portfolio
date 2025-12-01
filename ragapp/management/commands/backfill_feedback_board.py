# ragapp/management/commands/backfill_feedback_board.py
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction, models

from ragapp.models import (
    FeedbackLog,
    FeedbackReview,
    Feedback,
    QaragFeedback,
    ChatQueryLog,  # ✅ ChatQueryLog 도 같이 사용
)


class Command(BaseCommand):
    """
    1) 기존 FeedbackLog → FeedbackReview 가 없는 것들에 리뷰 자동 생성
    2) 레거시 테이블(Feedback, QaragFeedback, ChatQueryLog)에 쌓인 피드백을
       새 FeedbackLog + FeedbackReview 로 옮기는 백필 스크립트

    - stage 필드에:
      · legacy-fb-<id>
      · legacy-qa-<id>
      · legacy-chatlog-<id>
      를 넣어서, 여러 번 실행해도 중복 생성 안 되게 함.
    """

    help = "기존 피드백들을 통합 피드백 보드(FeedbackLog + FeedbackReview)로 옮깁니다."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="실제 저장 없이 개수만 출력",
        )
        parser.add_argument(
            "--skip-legacy",
            action="store_true",
            help=(
                "Feedback / QaragFeedback / ChatQueryLog 에서 가져오는 부분은 건너뛰고, "
                "FeedbackLog → FeedbackReview 매핑만 실행"
            ),
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        skip_legacy = options["skip_legacy"]

        # ──────────────────────────────────────────────────────
        # 1) 기존 FeedbackLog 에 리뷰(FeedbackReview)가 없으면 생성
        # ──────────────────────────────────────────────────────
        self.stdout.write(self.style.NOTICE("1) FeedbackLog → FeedbackReview 매핑 중..."))
        created_from_log = 0

        for log in FeedbackLog.objects.all().iterator():
            if hasattr(log, "review"):
                continue
            created_from_log += 1
            if not dry_run:
                FeedbackReview.objects.create(
                    feedback=log,
                    status="todo",
                )

        self.stdout.write(
            self.style.SUCCESS(f"   새로 생성된 리뷰(기존 로그 기준): {created_from_log}건")
        )

        if skip_legacy:
            self.stdout.write(
                self.style.WARNING("   (--skip-legacy 옵션으로 레거시 백필은 건너뜀)")
            )
            return

        # ──────────────────────────────────────────────────────
        # 2) 레거시 모델 → FeedbackLog + Review
        #    2-1: Feedback
        #    2-2: QaragFeedback
        #    2-3: ChatQueryLog (여기에 예전 코멘트가 박혀있을 가능성 큼)
        # ──────────────────────────────────────────────────────
        self.stdout.write(self.style.NOTICE("2) 레거시 테이블에서 백필 중..."))

        created_fb_logs = 0
        created_fb_reviews = 0
        created_qa_logs = 0
        created_qa_reviews = 0
        created_cq_logs = 0
        created_cq_reviews = 0

        # ── 2-1) Feedback (웹 / RAG 패널용 옛 테이블)
        fb_qs = Feedback.objects.all()

        # answer_type 매핑: 기존 모델은 gemini / rag / other
        ANSWER_TYPE_MAP = {
            "gemini": "web",   # 웹 검색 패널
            "rag": "rag",      # 내 자료 RAG
        }

        with transaction.atomic():
            for fb in fb_qs.iterator():
                stage_value = f"legacy-fb-{fb.pk}"

                existing = FeedbackLog.objects.filter(stage=stage_value).first()

                if dry_run:
                    if not existing:
                        created_fb_logs += 1
                        created_fb_reviews += 1
                    continue

                if existing:
                    # 여기선 딱히 업데이트할 내용 없음
                    continue

                answer_type = ANSWER_TYPE_MAP.get(fb.answer_type, "web")

                log = FeedbackLog.objects.create(
                    answer_type=answer_type,
                    from_ui="legacy_feedback_model",
                    question=fb.question or "",
                    answer=fb.answer or "",
                    sources=fb.sources_json or [],
                    helpful=fb.is_helpful,
                    reasons=[],
                    comment="",  # 옛 Feedback 모델에는 코멘트 필드가 따로 없었음
                    stage=stage_value,
                )
                # created_at 을 원래 Feedback.created_at 으로 맞춰주기
                FeedbackLog.objects.filter(pk=log.pk).update(created_at=fb.created_at)

                FeedbackReview.objects.create(
                    feedback=log,
                    status="todo",
                )

                created_fb_logs += 1
                created_fb_reviews += 1

        # ── 2-2) QaragFeedback (질문 챗봇 전용 옛 테이블)
        qa_qs = QaragFeedback.objects.filter(
            models.Q(is_helpful__isnull=False) | ~models.Q(comment="")
        )

        with transaction.atomic():
            for qa in qa_qs.iterator():
                stage_value = f"legacy-qa-{qa.pk}"

                existing = FeedbackLog.objects.filter(stage=stage_value).first()

                if dry_run:
                    if not existing:
                        created_qa_logs += 1
                        created_qa_reviews += 1
                    continue

                if existing:
                    # ✅ 예전에 만든 로그인데 comment 가 비어 있고,
                    #    QaragFeedback.comment 에 내용이 있으면 채워 넣기
                    updated = False
                    if (not existing.comment) and qa.comment:
                        existing.comment = qa.comment
                        updated = True
                    if updated:
                        existing.save(update_fields=["comment"])
                    continue

                log = FeedbackLog.objects.create(
                    answer_type="qa",               # 질문 챗봇 채널
                    from_ui="legacy_qarag_feedback",
                    question=qa.question or "",
                    answer=qa.answer or "",
                    sources=[],                     # 예전 테이블에는 소스 정보 없음
                    helpful=qa.is_helpful,
                    reasons=[],
                    comment=qa.comment or "",
                    stage=stage_value,
                )
                FeedbackLog.objects.filter(pk=log.pk).update(created_at=qa.created_at)

                FeedbackReview.objects.create(
                    feedback=log,
                    status="todo",
                )

                created_qa_logs += 1
                created_qa_reviews += 1

        # ── 2-3) ChatQueryLog (여기에 예전 QARAG/Web/RAG 피드백 코멘트가 있을 확률 높음)
        #       was_helpful 이 있거나 feedback(코멘트)이 비어있지 않은 것만 대상
        cq_qs = ChatQueryLog.objects.filter(
            models.Q(was_helpful__isnull=False) | ~models.Q(feedback="")
        )

        with transaction.atomic():
            for row in cq_qs.iterator():
                stage_value = f"legacy-chatlog-{row.pk}"

                existing = FeedbackLog.objects.filter(stage=stage_value).first()

                if dry_run:
                    if not existing:
                        created_cq_logs += 1
                        created_cq_reviews += 1
                    continue

                if existing:
                    # ✅ 기존 로그가 있는데 comment / helpful 를 덜 채웠으면 보충
                    updated_fields = []
                    if (not existing.comment) and row.feedback:
                        existing.comment = row.feedback
                        updated_fields.append("comment")
                    if (existing.helpful is None) and (row.was_helpful is not None):
                        existing.helpful = row.was_helpful
                        updated_fields.append("helpful")
                    if updated_fields:
                        existing.save(update_fields=updated_fields)
                    continue

                # 새로 생성해야 하는 경우
                # mode 기준으로 web / rag / qa 라벨 매핑
                if row.mode == "gemini":
                    answer_type = "web"
                elif row.mode == "rag":
                    answer_type = "rag"
                else:
                    answer_type = "qa"

                log = FeedbackLog.objects.create(
                    answer_type=answer_type,
                    from_ui="legacy_chatquerylog",
                    question=row.question or "",
                    answer=row.answer_excerpt or "",
                    sources=row.sources or [],
                    helpful=row.was_helpful,
                    reasons=[],
                    comment=row.feedback or "",
                    stage=stage_value,
                )
                FeedbackLog.objects.filter(pk=log.pk).update(created_at=row.created_at)

                FeedbackReview.objects.create(
                    feedback=log,
                    status="todo",
                )

                created_cq_logs += 1
                created_cq_reviews += 1

        # ──────────────────────────────────────────────────────
        # 요약 출력
        # ──────────────────────────────────────────────────────
        self.stdout.write("")
        prefix = "[DRY-RUN] " if dry_run else ""

        self.stdout.write(
            self.style.SUCCESS(
                prefix
                + f"Feedback → FeedbackLog: {created_fb_logs}건, "
                  f"FeedbackReview: {created_fb_reviews}건"
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                prefix
                + f"QaragFeedback → FeedbackLog: {created_qa_logs}건, "
                  f"FeedbackReview: {created_qa_reviews}건"
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                prefix
                + f"ChatQueryLog → FeedbackLog: {created_cq_logs}건, "
                  f"FeedbackReview: {created_cq_reviews}건"
            )
        )
        self.stdout.write(self.style.SUCCESS(prefix + "백필 작업 완료"))
