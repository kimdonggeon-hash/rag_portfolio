# ragpanel/admin.py
from django.contrib import admin
from django.contrib.auth.models import User, Group
from ragapp.models import MyLog, RagSetting


class RAGAdminSite(admin.AdminSite):
    site_header = "RAG 관리 콘솔"
    site_title = "RAG Admin"
    index_title = "RAG 관리 대시보드"

admin_site = RAGAdminSite(name="ragadmin")

# 기본 auth 모델도 이 콘솔에서 본다
admin_site.register(User)
admin_site.register(Group)


@admin.register(RagSetting, site=admin_site)
class RagSettingAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "auto_ingest_after_gemini",
        "web_ingest_to_chroma",
        "rag_force_answer",
        "rag_query_topk",
        "rag_fallback_topk",
        "rag_max_sources",
        "news_topk",
        "chroma_collection",
        "updated_at",
    )
    readonly_fields = ("updated_at",)
    fieldsets = (
        ("동작 플래그", {
            "fields": (
                "auto_ingest_after_gemini",
                "web_ingest_to_chroma",
                "rag_force_answer",
            )
        }),
        ("검색 파라미터", {
            "fields": (
                "rag_query_topk",
                "rag_fallback_topk",
                "rag_max_sources",
                "news_topk",
            )
        }),
        ("Chroma 안내", {
            "fields": (
                "chroma_db_dir",
                "chroma_collection",
            )
        }),
        ("기타", {
            "fields": ("updated_at",),
        }),
    )


@admin.register(MyLog, site=admin_site)
class MyLogAdmin(admin.ModelAdmin):
    list_display  = ("id", "mode", "short_q", "ok", "created_at", "remote_addr")
    list_filter   = ("mode", "ok", "created_at")
    search_fields = ("question", "answer_head", "error", "remote_addr", "user_agent")
    readonly_fields = ("created_at",)

    fieldsets = (
        (None, {"fields": ("mode", "ok", "error")}),
        ("질문/답변", {"fields": ("question", "answer_head")}),
        ("메타/클라이언트", {"fields": ("meta", "remote_addr", "user_agent", "created_at")}),
    )

    def short_q(self, obj):
        return (obj.question or "")[:40]
    short_q.short_description = "질문(앞부분)"
