# ragapp/admin_site.py
from __future__ import annotations

import os
from pathlib import Path

from django.conf import settings
from django.contrib.admin import AdminSite
from django.shortcuts import render, redirect
from django.urls import path, reverse
from ragapp.news_views.views_crawl import crawl_news_view as crawl_news_admin_view

# 커스텀 운영 화면들 (뉴스 크롤링, 업로드, FAQ, 라이브챗, 법무)
from ragapp import admin_views

from ragapp.news_views.upload_views import upload_doc_view as upload_doc_page_view

class RagAdminSite(AdminSite):
    """
    /ragadmin/ 전용 관리 콘솔 사이트
    - index: ragadmin/dashboard.html (애플 감성 대시보드)
    - 각종 서브 페이지는 ragapp.admin_views.* 에 위임
    """

    site_header = "RAG Admin"
    site_title = "RAG Admin"
    index_title = "RAG Admin 대시보드"
    index_template = "ragadmin/dashboard.html"

    # ──────────────────────────────────────────────────────────────
    # 공통 컨텍스트: 벡터 DB / Chroma / AUTO_INGEST 플래그
    # ──────────────────────────────────────────────────────────────
    def each_context(self, request):
        ctx = super().each_context(request)
        base_dir = getattr(settings, "BASE_DIR", Path("."))

        # AUTO_INGEST_AFTER_GEMINI 값: settings 또는 ENV 기준으로 bool 처리
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

    # ──────────────────────────────────────────────────────────────
    # index: /ragadmin/ 대시보드
    # ──────────────────────────────────────────────────────────────
    def index(self, request, extra_context=None):
        """
        기본 AdminSite.index 를 살짝 커스터마이즈:
        - our each_context() + app_list + dashboard.html 사용
        """
        context = self.each_context(request)
        if extra_context:
            context.update(extra_context)

        # 기본 admin index 와 동일하게 app_list 붙여줌
        context["title"] = self.index_title
        context["app_list"] = self.get_app_list(request)

        request.current_app = self.name
        return render(request, self.index_template, context)

    # ──────────────────────────────────────────────────────────────
    # 내부 이동(대시보드에서 POST로 호출하는 goto 버튼용)
    # ──────────────────────────────────────────────────────────────
    def goto_view(self, request):
        """
        대시보드에서 내부 섹션으로 이동하기 위한 POST 엔드포인트
        dest 키에 따라 적절한 URL 네임으로 redirect
        """
        if request.method != "POST":
            # 잘못된 접근은 가이드로 보냄
            return redirect(reverse("ragadmin:guide"))

        dest = (request.POST.get("dest") or "").strip()

        mapping = {
            # 커스텀 화면 (ragadmin 네임스페이스)
            "crawl-news": "ragadmin:crawl_news",
            "upload-doc": "ragadmin:upload_doc",
            "faq-suggest": "ragadmin:faq_suggest",
            "live-chat": "ragadmin:live_chat",
            "legal": "ragadmin:legal_entry",
            "guide": "ragadmin:guide",

            # ✅ 피드백 보드
            "feedback-board": "ragadmin:feedback_board",

            # 모델 목록(아래 2번 참고)
            "ragsetting_list": "ragadmin:ragapp_ragsetting_changelist",
            "mylog_list": "ragadmin:ragapp_mylog_changelist",
            "chatquerylog_list": "ragadmin:ragapp_chatquerylog_changelist",
            "feedback_list": "ragadmin:ragapp_feedback_changelist",
            "ingesthistory_list": "ragadmin:ragapp_ingesthistory_changelist",
            "ragchunk_list": "ragadmin:ragapp_ragchunk_changelist",
            "faqentry_list": "ragadmin:ragapp_faqentry_changelist",
            "applog_list": "ragadmin:ragapp_applog_changelist",
        }
        target_name = mapping.get(dest)
        try:
            if target_name:
                return redirect(reverse(target_name))
        except Exception:
            pass
        # 매핑 실패 시 가이드로 폴백
        return redirect(reverse("ragadmin:guide"))

    # ──────────────────────────────────────────────────────────────
    # 가이드 래퍼 뷰 (/ragadmin/guide/)
    # ──────────────────────────────────────────────────────────────
    def guide_view(self, request):
        """
        관리자용 '이용 가이드' 래퍼 화면
        - 템플릿: ragadmin/guide.html
        - 내부에서 /guide 또는 legal_guide 링크를 노출
        """
        ctx = self.each_context(request)
        return render(request, "ragadmin/guide.html", ctx)

    # ──────────────────────────────────────────────────────────────
    # URL 라우팅
    # ──────────────────────────────────────────────────────────────
    def get_urls(self):
        """
        /ragadmin/ 아래에 붙는 커스텀 라우트들
        (namespaced: 'ragadmin:...')
        """
        extra = [
            # 가이드 / 대시보드 내부 이동
            path("guide/", self.admin_view(self.guide_view), name="guide"),
            path("goto/", self.admin_view(self.goto_view), name="goto"),

            # 크롤링 / 업로드 / FAQ / 라이브챗 / 법무 설정
            path("crawl-news/", self.admin_view(crawl_news_admin_view), name="crawl_news"),
            path("upload-doc/", self.admin_view(upload_doc_page_view), name="upload_doc"),
            path("faq-suggest/", self.admin_view(admin_views.faq_suggest_view), name="faq_suggest"),
            path(
                "faq-suggest/promote/",
                self.admin_view(admin_views.faq_promote_view),
                name="faq_promote",
            ),
            path("live-chat/", self.admin_view(admin_views.live_chat_view), name="live_chat"),
            path(
                "legal/",
                self.admin_view(admin_views.legal_config_entrypoint),
                name="legal_entry",
            ),

            # ✅ 여기 한 줄 추가
            path(
                "feedback-board/",
                self.admin_view(admin_views.feedback_board_view),
                name="feedback_board",
            ),

        ]
        # AdminSite 기본 URL 들 뒤에 커스텀 라우트들을 앞에 붙여서 우선순위 확보
        return extra + super().get_urls()


# 전역 인스턴스 (ragsite/urls.py에서 사용)
rag_admin_site = RagAdminSite(name="ragadmin")
