# ragapp/admin_chat_retention.py
from __future__ import annotations

from django.contrib import admin
from django.utils import timezone
from . import admin_chat_retention

from ragapp.models_chat_retention import (
    LiveChatMessage,
    ChatEvidence,
    RetentionClass,
    compute_purge_at,
    LiveChatAbuseKeyword,
)


@admin.action(description="선택 메시지: 욕설/모욕 증빙으로 전환(증빙 생성 + 보관기간 연장)")
def make_abuse_evidence(modeladmin, request, queryset):
    now = timezone.now()
    for m in queryset:
        # 증빙 레코드 생성
        ChatEvidence.objects.create(
            session=m.session,
            message=m,
            captured_text=m.content,
            reason=(m.flag_reason or "manual"),
            created_by=request.user,
        )
        # 보존 클래스/플래그 업데이트
        m.retention_class = RetentionClass.ABUSE
        m.flagged_at = m.flagged_at or now
        m.flag_reason = m.flag_reason or "manual:admin"
        m.flagged_by = request.user
        m.purge_at = compute_purge_at(m.created_at, m.retention_class)
        m.save(
            update_fields=[
                "retention_class",
                "flagged_at",
                "flag_reason",
                "flagged_by",
                "purge_at",
            ]
        )


@admin.action(description="선택 메시지: 법무 홀드(파기 중단/최대기간 적용)")
def set_legal_hold(modeladmin, request, queryset):
    for m in queryset:
        m.retention_class = RetentionClass.LEGAL_HOLD
        m.flagged_at = m.flagged_at or timezone.now()
        m.flag_reason = m.flag_reason or "manual:legal_hold"
        m.flagged_by = request.user
        m.purge_at = compute_purge_at(m.created_at, m.retention_class)
        m.save(
            update_fields=[
                "retention_class",
                "flagged_at",
                "flag_reason",
                "flagged_by",
                "purge_at",
            ]
        )


@admin.action(description="선택 메시지: 홀드 해제(일반 30일로 복귀)")
def release_hold_to_normal(modeladmin, request, queryset):
    for m in queryset:
        m.retention_class = RetentionClass.NORMAL
        m.purge_at = compute_purge_at(m.created_at, m.retention_class)
        m.save(update_fields=["retention_class", "purge_at"])


@admin.register(LiveChatMessage)
class LiveChatMessageAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "session",
        "role",
        "retention_class",
        "created_at",
        "purge_at",
        "flag_reason",
    )
    list_filter = ("retention_class", "role")
    search_fields = ("content", "session__id")
    actions = [make_abuse_evidence, set_legal_hold, release_hold_to_normal]


@admin.register(ChatEvidence)
class ChatEvidenceAdmin(admin.ModelAdmin):
    list_display = ("id", "session", "created_at", "purge_at", "reason", "created_by")
    list_filter = ()
    search_fields = ("captured_text", "reason", "session__id")


@admin.register(LiveChatAbuseKeyword)
class LiveChatAbuseKeywordAdmin(admin.ModelAdmin):
    """
    상담 욕설/금지어 관리용 어드민
    - pattern: 키워드 또는 정규식
    - use_regex: 정규식 여부
    - is_active: 사용 여부
    """
    list_display = (
        "id",
        "pattern",
        "use_regex",
        "is_active",
        "note",
        "created_at",
        "updated_at",
    )
    list_filter = ("is_active", "use_regex")
    search_fields = ("pattern", "note")
    ordering = ("-created_at",)
