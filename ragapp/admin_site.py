# ragapp/admin_site.py
from __future__ import annotations

import os
from pathlib import Path

from django.conf import settings
from django.contrib.admin import AdminSite
from django.shortcuts import render, redirect
from django.urls import path, reverse
from django.utils.module_loading import import_string


class RagAdminSite(AdminSite):
    site_header = "RAG Admin"
    site_title = "RAG Admin"
    index_title = "RAG Admin 대시보드"
    index_template = "ragadmin/dashboard.html"

    def each_context(self, request):
        ctx = super().each_context(request)
        base_dir = getattr(settings, "BASE_DIR", Path("."))

        auto_ingest_raw = (
            getattr(settings, "AUTO_INGEST_AFTER_GEMINI", None)
            or os.environ.get("AUTO_INGEST_AFTER_GEMINI")
            or "1"
        )
        auto_ingest_str = str(auto_ingest_raw).strip().lower()
        auto_ingest = auto_ingest_str not in ("0", "false", "no", "off", "", "none", "null")

        ctx.update(
            {
                "VECTOR_DB_PATH": os.environ.get("VECTOR_DB_PATH")
                or str(Path(base_dir) / "vector_store.sqlite3"),
                "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),
                "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
                "AUTO_INGEST_AFTER_GEMINI": auto_ingest,
            }
        )
        return ctx

    def index(self, request, extra_context=None):
        context = self.each_context(request)
        if extra_context:
            context.update(extra_context)

        context["title"] = self.index_title
        context["app_list"] = self.get_app_list(request)

        # Dashboard badges should show real zeroes on a fresh installation,
        # rather than looking like unfinished placeholders.
        try:
            from ragapp.machine.media_helpers import _list_pending_ids

            context["media_pending_count"] = len(_list_pending_ids(limit=500))
        except Exception:
            context["media_pending_count"] = 0

        try:
            from ragapp.moderation_models import UserPenalty

            penalty_qs = UserPenalty.objects.all()
            field_names = {f.name for f in UserPenalty._meta.get_fields()}
            if "is_active" in field_names:
                penalty_qs = penalty_qs.filter(is_active=True)
            context["media_penalty_count"] = penalty_qs.count()
        except Exception:
            context["media_penalty_count"] = 0

        request.current_app = self.name
        return render(request, self.index_template, context)

    def goto_view(self, request):
        if request.method != "POST":
            return redirect(reverse("ragadmin:guide"))

        dest = (request.POST.get("dest") or "").strip()

        mapping = {
            "crawl-news": "ragadmin:crawl_news",
            "upload-doc": "ragadmin:upload_doc",
            "faq-suggest": "ragadmin:faq_suggest",
            "live-chat": "ragadmin:live_chat",
            "legal": "ragadmin:legal_entry",
            "guide": "ragadmin:guide",
            "feedback-board": "ragadmin:feedback_board",
            "ragsetting_list": "ragadmin:ragapp_ragsetting_changelist",
            "mylog_list": "ragadmin:ragapp_mylog_changelist",
            "chatquerylog_list": "ragadmin:ragapp_chatquerylog_changelist",
            "feedback_list": "ragadmin:ragapp_feedback_changelist",
            "ingesthistory_list": "ragadmin:ragapp_ingesthistory_changelist",
            "ragchunk_list": "ragadmin:ragapp_ragchunk_changelist",
            "faqentry_list": "ragadmin:ragapp_faqentry_changelist",
            "applog_list": "ragadmin:ragapp_applog_changelist",
            "usage": "ragadmin:usage",
        }

        target_name = mapping.get(dest)
        try:
            if target_name:
                return redirect(reverse(target_name))
        except Exception:
            pass
        return redirect(reverse("ragadmin:guide"))

    def guide_view(self, request):
        ctx = self.each_context(request)
        return render(request, "ragadmin/guide.html", ctx)

    def get_urls(self):
        """
        ✅ 진짜 lazy: 서버 부팅 시점에 import하지 않고,
        요청이 들어온 순간에만 import_string으로 뷰를 로딩.
        (지금 터진 usage_views import 크래시 방지)
        """

        def _lazy_view(dotted_path: str):
            def _view(request, *args, **kwargs):
                view = import_string(dotted_path)
                return view(request, *args, **kwargs)
            return _view

        extra = [
            path("guide/", self.admin_view(self.guide_view), name="guide"),
            path("goto/", self.admin_view(self.goto_view), name="goto"),

            # ✅ 여기(usage)가 너 로그에서 터진 지점 → lazy로 고정
            path("usage/", self.admin_view(_lazy_view("ragapp.news_views.usage_views.ragadmin_usage_view")), name="usage"),

            path("crawl-news/", self.admin_view(_lazy_view("ragapp.news_views.views_crawl.crawl_news_view")), name="crawl_news"),
            path("upload-doc/", self.admin_view(_lazy_view("ragapp.news_views.upload_views.upload_doc_view")), name="upload_doc"),

            path("faq-suggest/", self.admin_view(_lazy_view("ragapp.admin_views.faq_suggest_view")), name="faq_suggest"),
            path("faq-suggest/promote/", self.admin_view(_lazy_view("ragapp.admin_views.faq_promote_view")), name="faq_promote"),

            path("live-chat/", self.admin_view(_lazy_view("ragapp.admin_views.live_chat_view")), name="live_chat"),
            path("legal/", self.admin_view(_lazy_view("ragapp.admin_views.legal_config_entrypoint")), name="legal_entry"),
            path("feedback-board/", self.admin_view(_lazy_view("ragapp.admin_views.feedback_board_view")), name="feedback_board"),
        ]
        return extra + super().get_urls()


rag_admin_site = RagAdminSite(name="ragadmin")
