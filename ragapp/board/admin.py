# ragapp/board/admin.py
from __future__ import annotations

from django.contrib import admin

from .models import (
    BoardCategory,
    BoardPost,
    BoardComment,
    BoardReport,
    BoardAbuseKeyword,
    BoardAdminActionLog,
)


@admin.register(BoardCategory)
class BoardCategoryAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "slug", "order", "is_notice")
    list_filter = ("is_notice",)
    search_fields = ("name", "slug")
    ordering = ("order", "name")
    list_editable = ("order", "is_notice")


@admin.register(BoardPost)
class BoardPostAdmin(admin.ModelAdmin):
    list_display = (
        "id", "title", "category",
        "pinned", "allow_comments", "is_published", "is_deleted",
        "view_count", "created_at",
    )
    list_filter = ("category", "pinned", "allow_comments", "is_published", "is_deleted")
    search_fields = ("title", "body", "guest_name", "author__username")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    readonly_fields = ("created_at", "updated_at", "deleted_at")


@admin.register(BoardComment)
class BoardCommentAdmin(admin.ModelAdmin):
    list_display = ("id", "post", "is_hidden", "is_deleted", "created_at")
    list_filter = ("is_hidden", "is_deleted")
    search_fields = ("body", "guest_name", "author__username", "post__title")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    readonly_fields = ("created_at", "updated_at", "hidden_at", "deleted_at")


@admin.register(BoardReport)
class BoardReportAdmin(admin.ModelAdmin):
    list_display = ("id", "target_type", "post", "comment", "reason", "status", "created_at")
    list_filter = ("target_type", "status", "reason")
    search_fields = ("message", "admin_note", "reporter_fp", "reporter__username")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    readonly_fields = ("created_at", "handled_at")


@admin.register(BoardAbuseKeyword)
class BoardAbuseKeywordAdmin(admin.ModelAdmin):
    list_display = ("id", "pattern", "is_regex", "enabled", "created_at")
    list_filter = ("enabled", "is_regex")
    search_fields = ("pattern",)
    ordering = ("-created_at",)
    readonly_fields = ("created_at",)


@admin.register(BoardAdminActionLog)
class BoardAdminActionLogAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "action", "done", "actor_name", "status", "auto_only")
    list_filter = ("action", "status", "auto_only")
    search_fields = ("actor_name", "q", "note", "report_ids_preview", "path", "query_string")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    readonly_fields = ("created_at",)
