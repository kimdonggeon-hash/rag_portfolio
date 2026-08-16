# ragsite/urls.py
from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.conf.urls.static import static as djstatic
from django.contrib import admin
from django.urls import include, path
from django.views.generic.base import RedirectView

from ragsite.url_helpers import (
    chrome_devtools_manifest,
    favicon,
    hello,
    missing_view,
)

from ragapp.media_views import uploads_proxy, uploads_probe, pending_raw_proxy, rejected_raw_proxy

from ragapp.news_views.usage_views import daily_usage_dashboard, api_usage_status
from ragapp import admin_views, views_feedback
from ragapp.livechat import views as livechat_views
from ragapp.news_views import pdf_views
from ragapp.views_legal_modal import legal_modal_fragment
from ragapp import views_ops_control

from ragapp.admin_site import rag_admin_site

from ragapp.news_views.news_views import (
    assistant_view,
    home,
    news,
    qa_rag_chat,
    qarag_live_chat_request,
    rag_qa_view,
    web_qa_view,
)

from ragapp.news_views.views_crawl import api_news_ingest, crawl_news_view
from ragapp.news_views.upload_views import upload_doc_view
from ragapp.news_views.policy_qa_views import qa_policy_view

from ragapp.feature_views import (
    # media
    media_index_view,
    media_search_view,
    media_pending_admin_view,
    media_rejected_admin_view,
    media_upload_admin_view,
    media_penalties_admin_view,
    api_media_upsert_tags_caption,
    api_media_keyword_search,
    api_media_pending_list,
    api_media_pending_approve,
    api_media_pending_reject,
    api_media_rejected_delete,
    api_user_penalty_list,
    api_user_penalty_lift,

    # table (✅ admin-only import만 유지)
    table_index_admin_view,
    table_search_admin_view,

    # ✅ table legacy redirect (name/table_search 깨짐 방지)
    table_index_view,
    table_search_view,
)


from ragapp.news_views.api_views import (
    api_chroma_verify,
    api_config,
    api_diag,
    api_legal_bundle,
    api_media_upsert,
    api_ping,
    api_rag_diag,
    api_rag_search,
    api_rag_seed,
    api_rag_upsert,
    api_vector_diag,
    api_vector_verify,
)

from ragapp.views_admin_obs_badge import (
    obsbadge_status,
    obsbadge_toggle,
    obsbadge_config,
    obsbadge_health,
    obsbadge_history_clear,
)

from ragapp.legal_views import consent_record, healthz, privacy_page, robots_txt
from ragsite.legal_url_views import (
    legal_bundle,
    legal_guide,
    legal_overseas,
    legal_privacy,
    legal_tester,
    legal_tos,
)


urlpatterns = [
    # ======================================================================
    # ✅ (중요) admin.site.urls 보다 먼저 와야 함
    # ======================================================================
    path("admin/obsbadge/status/", obsbadge_status, name="obsbadge_status"),
    path("admin/obsbadge/toggle/", obsbadge_toggle, name="obsbadge_toggle"),
    path("admin/obsbadge/config/", obsbadge_config, name="obsbadge_config"),
    path("admin/obsbadge/health/", obsbadge_health, name="obsbadge_health"),
    path("admin/obsbadge/history/clear/", obsbadge_history_clear, name="obsbadge_history_clear"),

    # ----------------------------------------------------------------------
    # 기본 페이지
    # ----------------------------------------------------------------------
    path("", home, name="home"),
    path("news/", news, name="news"),
    path("assistant/", assistant_view, name="assistant_view"),

    # ----------------------------------------------------------------------
    # Django 기본 Admin
    # ----------------------------------------------------------------------
    path("admin/", admin.site.urls),

    # ----------------------------------------------------------------------
    # ✅ 업로드 이미지 서빙 (probe를 위로) + 슬래시 호환
    # ----------------------------------------------------------------------
    path("uploads/probe", uploads_probe, name="uploads_probe"),
    path("uploads/probe/", uploads_probe),
    path("uploads/<path:key>", uploads_proxy, name="uploads_proxy"),
    path("uploads/<path:key>/", uploads_proxy),

    # ----------------------------------------------------------------------
    # API (슬래시 유무 호환 강화)
    # ----------------------------------------------------------------------
    path("api/ping", api_ping, name="api_ping"),
    path("api/ping/", api_ping),

    path("api/config", api_config, name="api_config"),
    path("api/config/", api_config),

    path("api/diag", api_diag, name="api_diag"),
    path("api/diag/", api_diag),

    path("api/media/keyword_search", api_media_keyword_search),
    path("api/media/keyword_search/", api_media_keyword_search, name="api_media_keyword_search"),
    path("api/media/upsert", api_media_upsert, name="api_media_upsert"),
    path("api/media/upsert/", api_media_upsert),

    path("api/media/upsert_tags", api_media_upsert_tags_caption, name="api_media_upsert_tags"),
    path("api/media/upsert_tags/", api_media_upsert_tags_caption),

    path("ragadmin/media/upload/", media_upload_admin_view, name="ragadmin_media_upload"),

    # pending 검수 페이지
    path("ragadmin/media/pending", RedirectView.as_view(url="/ragadmin/media/pending/", permanent=False)),
    path("ragadmin/media/pending/", media_pending_admin_view, name="ragadmin_media_pending"),

    # ✅ (추가) pending 미리보기 raw (id 기반)
    path("ragadmin/media/pending/raw/<str:item_id>", pending_raw_proxy, name="ragadmin_media_pending_raw"),
    path("ragadmin/media/pending/raw/<str:item_id>/", pending_raw_proxy),

    path("ragadmin/media/rejected/", media_rejected_admin_view, name="ragadmin_media_rejected"),
    path("ragadmin/media/rejected/raw/<str:item_id>", rejected_raw_proxy, name="ragadmin_media_rejected_raw"),
    path("ragadmin/media/rejected/raw/<str:item_id>/", rejected_raw_proxy),

    # pending 목록
    path("api/media/pending", api_media_pending_list, name="api_media_pending_list"),
    path("api/media/pending/", api_media_pending_list),

    # pending 승인/거절
    path("api/media/pending/approve", api_media_pending_approve, name="api_media_pending_approve"),
    path("api/media/pending/approve/", api_media_pending_approve),

    path("api/media/pending/reject", api_media_pending_reject, name="api_media_pending_reject"),
    path("api/media/pending/reject/", api_media_pending_reject),

    path("api/media/rejected/delete", api_media_rejected_delete, name="api_media_rejected_delete"),
    path("api/media/rejected/delete/", api_media_rejected_delete),

    # ✅ (추가) 선처(정지/제재 해제) 화면 + API
    path("ragadmin/media/penalties/", media_penalties_admin_view, name="ragadmin_media_penalties"),
    path("api/moderation/penalty/lift", api_user_penalty_lift, name="api_user_penalty_lift"),
    path("api/moderation/penalty/lift/", api_user_penalty_lift),

    path("api/moderation/penalty/list", api_user_penalty_list, name="api_user_penalty_list"),
    path("api/moderation/penalty/list/", api_user_penalty_list),

    path("api/web_qa", web_qa_view or missing_view("web_qa_view"), name="api_web_qa"),
    path("api/web_qa/", web_qa_view or missing_view("web_qa_view")),

    path("api/rag_qa", rag_qa_view, name="api_rag_qa"),
    path("api/rag_qa/", rag_qa_view),

    path("api/qa_policy", qa_policy_view, name="qa_policy_view"),
    path("api/qa_policy/", qa_policy_view),

    path("api/news_ingest", api_news_ingest, name="api_news_ingest"),
    path("api/news_ingest/", api_news_ingest),

    path("api/rag/seed", api_rag_seed, name="api_rag_seed"),
    path("api/rag/upsert", api_rag_upsert, name="api_rag_upsert"),
    path("api/rag/search", api_rag_search, name="api_rag_search"),
    path("api/rag/diag", api_rag_diag, name="api_rag_diag"),

    path("api/chroma/verify", api_chroma_verify, name="api_chroma_verify"),
    path("api/chroma/verify/", api_chroma_verify),

    path("api/vector_diag", api_vector_diag, name="api_vector_diag"),
    path("api/vector_diag/", api_vector_diag),
    path("api/vector_verify", api_vector_verify, name="api_vector_verify"),
    path("api/vector_verify/", api_vector_verify),
    path("api/vector/diag", api_vector_diag),
    path("api/vector/diag/", api_vector_diag),
    path("api/vector/verify", api_vector_verify),
    path("api/vector/verify/", api_vector_verify),

    path("api/feedback", views_feedback.api_submit_feedback, name="api_feedback"),
    path("api/feedback/", views_feedback.api_submit_feedback),
    path("api/submit_feedback", views_feedback.api_submit_feedback, name="submit_feedback"),
    path("api/submit_feedback/", views_feedback.api_submit_feedback),
    path("submit_feedback", views_feedback.api_submit_feedback, name="submit_feedback_legacy"),
    path("submit_feedback/", views_feedback.api_submit_feedback),

    path("api/legal_bundle", api_legal_bundle, name="api_legal_bundle"),
    path("api/legal_bundle/", api_legal_bundle),

    path("api/pdf-analyze/", pdf_views.pdf_analyze_api_view, name="api_pdf_analyze"),

    path("api/usage/status", api_usage_status),
    path("api/usage/status/", api_usage_status, name="api_usage_status"),

    # ----------------------------------------------------------------------
    # 페이지/리다이렉트 (슬래시 호환 일부 추가)
    # ----------------------------------------------------------------------
    path("hello", hello),
    path("hello/", hello),

    path("healthz", healthz, name="healthz"),
    path("healthz/", healthz),

    path("robots.txt", robots_txt, name="robots_txt"),
    path("favicon.ico", favicon, name="favicon"),
    path(".well-known/appspecific/com.chrome.devtools.json", chrome_devtools_manifest),

    path("legal/modal-fragment/", legal_modal_fragment, name="legal_modal_fragment"),
    path("legal/privacy/", legal_privacy, name="legal_privacy"),
    path("legal/tos/", legal_tos, name="legal_tos"),
    path("legal/overseas/", legal_overseas, name="legal_overseas"),
    path("legal/tester/", legal_tester, name="legal_tester"),
    path("legal/bundle", legal_bundle, name="legal_bundle"),
    path("legal/bundle/", legal_bundle),
    path("guide", legal_guide, name="legal_guide"),
    path("guide/", legal_guide),

    path("privacy", RedirectView.as_view(url="/legal/privacy/", permanent=False), name="privacy"),
    path("privacy/", RedirectView.as_view(url="/legal/privacy/", permanent=False)),
    path("legal/privacy-min/", privacy_page, name="privacy_page_min"),

    path("api/consent", consent_record, name="consent_record"),
    path("api/consent/", consent_record),
    path("legal/consent/confirm", consent_record, name="legal_consent_confirm"),
    path("legal/consent/confirm/", consent_record),

    path("media/index", RedirectView.as_view(url="/media/index/", permanent=False)),
    path("media/index/", media_index_view, name="media_index"),
    path("media/search", RedirectView.as_view(url="/media/search/", permanent=False)),
    path("media/search/", media_search_view, name="media_search"),

    # ----------------------------------------------------------------------
    # RAG QA (name 중복 제거)
    # ----------------------------------------------------------------------
    path("rag-qa", rag_qa_view, name="rag_qa_page"),
    path("rag-qa/", rag_qa_view),
    path("rag_qa/", rag_qa_view, name="rag_qa_page_slash"),
    path("rag_qa", RedirectView.as_view(url="/rag_qa/", permanent=False), name="rag_qa_legacy"),

    path("qa-rag-chat/", qa_rag_chat, name="qa_rag_chat"),

    # ----------------------------------------------------------------------
    # Livechat API (직접 선언 유지)
    # ----------------------------------------------------------------------
    path("api/live_chat/request", qarag_live_chat_request, name="qarag_live_chat_request"),
    path("api/live_chat/request/", qarag_live_chat_request),
    path("livechat/api/request/", livechat_views.livechat_request_api),
    path("livechat/api/end/", livechat_views.api_livechat_end),

    # ----------------------------------------------------------------------
    # table legacy (✅ name/table_search 깨짐 방지 → admin으로 보냄, 쿼리 보존)
    # ----------------------------------------------------------------------
    path("table/index", table_index_view),
    path("table/index/", table_index_view, name="table_index"),
    path("table/search", table_search_view),
    path("table/search/", table_search_view, name="table_search"),

    # ----------------------------------------------------------------------
    # ragadmin
    # ----------------------------------------------------------------------
    path("ragadmin/table/index/", table_index_admin_view, name="ragadmin_table_index"),
    path("ragadmin/table/search/", table_search_admin_view, name="ragadmin_table_search"),

    path("ragadmin/upload-doc/", upload_doc_view, name="ragadmin_upload_doc"),

    path("ragadmin/crawl-news", RedirectView.as_view(url="/ragadmin/crawl-news/", permanent=False)),
    path("ragadmin/crawl-news/", crawl_news_view, name="ragadmin_crawl_news"),

    path("ragadmin/usage/", daily_usage_dashboard, name="ragadmin_usage"),
    path("ragadmin/runtime/", admin_views.runtime_dashboard, name="runtime_dashboard"),

    path("ragadmin/ops/api/status/", views_ops_control.ops_status, name="ops_status"),
    path("ragadmin/ops/api/set/", views_ops_control.ops_set, name="ops_set"),
    path("ragadmin/ops/api/reset-presence/", views_ops_control.ops_reset_presence, name="ops_reset_presence"),

    # ✅ live-chat: 슬래시 보정 + 실제 뷰 연결 (self-redirect 제거)
    path("ragadmin/live-chat", RedirectView.as_view(url="/ragadmin/live-chat/", permanent=False)),
    path("ragadmin/live-chat/", livechat_views.live_chat_view, name="ragadmin_live_chat"),
    path("ragadmin/livechat/", RedirectView.as_view(url="/ragadmin/live-chat/", permanent=False), name="ragadmin_live_chat_legacy"),

    # ✅ (레거시) save-session은 정식 저장 API로 별칭 연결
    path("ragadmin/live-chat/save-session/", livechat_views.api_livechat_save, name="live_chat_save_session_legacy"),

    path("ragadmin/", rag_admin_site.urls),

    # ----------------------------------------------------------------------
    # 기타
    # ----------------------------------------------------------------------
    path("api/qarag/feedback/", views_feedback.api_qarag_feedback, name="api_qarag_feedback"),
    path("api/qarag/feedback", views_feedback.api_qarag_feedback),

    path("board/", include(("ragapp.board.urls", "board"), namespace="board")),

    # ----------------------------------------------------------------------
    # ⚠️ include("")는 마지막
    # ----------------------------------------------------------------------
    path("", include(("ragapp.livechat.urls", "livechat"), namespace="livechat")),
]

if settings.DEBUG:
    urlpatterns += djstatic(
        settings.STATIC_URL,
        document_root=str((Path(settings.BASE_DIR) / "ragapp" / "static").resolve()),
    )
