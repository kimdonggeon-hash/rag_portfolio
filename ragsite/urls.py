# ragsite/urls.py
from django.contrib import admin
from django.urls import path, include
from django.http import HttpResponse, JsonResponse
from django.templatetags.static import static as static_url
from django.views.generic.base import RedirectView
from django.conf import settings
from django.conf.urls.static import static as djstatic
from pathlib import Path

from ragapp import views_feedback
from ragapp.news_views import pdf_views

# livechat 뷰
from ragapp.livechat import views as livechat_views

# ✅ RAG 전용 어드민 사이트
from ragapp.admin_site import rag_admin_site

# ✅ 화면/페이지 뷰
from ragapp.news_views.news_views import (
    home,
    news,
    web_qa_view,         # POST
    rag_qa_view,         # POST 및 페이지
    qa_rag_chat,
    assistant_view,
    qarag_live_chat_request,   # QARAG → 상담 요청 API
)

# ✅ crawl-news 분리
from ragapp.news_views.views_crawl import (
    crawl_news_view,
    api_news_ingest,
)

# ⬇ 업로드 전용 뷰는 분리된 모듈에서 import
from ragapp.news_views.upload_views import upload_doc_view

# ✅ 기능 데모(이미지/표)
from ragapp.feature_views import (
    media_index_view,
    media_search_view,
    table_index_view,
    table_search_view,
)

# ✅ API 묶음
from ragapp.news_views.api_views import (
    api_ping,
    api_config,
    api_diag,
    api_chroma_verify,
    api_rag_seed,
    api_rag_diag,
    api_rag_upsert,
    api_rag_search,
    api_feedback,
    api_vector_diag,
    api_vector_verify,
    api_legal_bundle,   # /api/legal_bundle JSON
    api_ingest_news as api_ingest_news_pipeline,  # 파이프라인용 별도 엔드포인트(유지)
)

# 🔐 법적/헬스체크(어댑터)
from ragapp.legal_views import robots_txt, privacy_page, healthz, consent_record

# 🔐 법적 문서 뷰(뉴스 뷰즈 쪽) + 가이드
try:
    from ragapp.news_views.legal_views import (
        legal_privacy,
        legal_tos,
        legal_overseas,
        legal_tester,
        legal_guide,
        legal_bundle,
    )
except Exception:
    def _legal_stub(_req, what="문서"):
        return HttpResponse(f"{what} 페이지가 준비되지 않았습니다.", status=200)

    def legal_bundle(_req):
        return HttpResponse("{}", content_type="application/json")

    def legal_privacy(req):  return _legal_stub(req, "개인정보처리방침")
    def legal_tos(req):      return _legal_stub(req, "이용약관")
    def legal_overseas(req): return _legal_stub(req, "국외이전 고지")
    def legal_tester(req):   return _legal_stub(req, "테스터 안내")
    def legal_guide(req):    return _legal_stub(req, "이용 가이드")


# ✅ 운영자 페이지 모듈(뷰 안전 폴백)
from ragapp import admin_views


def _missing_view(name):
    def _view(_request, *args, **kwargs):
        return JsonResponse(
            {"ok": False, "error": f"{name} view not available"},
            status=501,
            json_dumps_params={"ensure_ascii": False},
        )
    return _view


admin_live_chat_view = getattr(admin_views, "live_chat_view", None) or _missing_view("live_chat_view")


# 레거시 클라이언트 호환용(없을 수 있음)
try:
    from ragapp.news_views.news_views import submit_feedback  # POST (레거시)
except Exception:
    submit_feedback = None


def hello(_request):
    return HttpResponse("urls.py 연결 OK")


def chrome_devtools_manifest(_request):
    return JsonResponse({}, status=200)


urlpatterns = [
    # 메인 홈
    path("", home, name="home"),
    path("news/", news, name="news"),

    # 엔드유저용 심플 챗 화면
    path("assistant/", assistant_view, name="assistant_view"),

    # 기본 장고 어드민
    path("admin/", admin.site.urls),

    # 상태 / 진단
    path("hello", hello),
    path("healthz", healthz, name="healthz"),
    path("api/ping", api_ping, name="api_ping"),
    path("api/config", api_config, name="api_config"),
    path("api/diag", api_diag, name="api_diag"),

    # 웹 패널용 QA (POST)
    path("api/web_qa",  web_qa_view or _missing_view("web_qa_view"), name="api_web_qa"),
    path("api/web_qa/", web_qa_view or _missing_view("web_qa_view")),

    # ✅ RAG QA API (POST)
    path("api/rag_qa",  rag_qa_view, name="api_rag_qa"),
    path("api/rag_qa/", rag_qa_view),

    # ✅ 뉴스 인덱싱 (crawl 분리된 모듈)
    path("api/news_ingest",  api_news_ingest, name="api_news_ingest"),
    path("api/news_ingest/", api_news_ingest),

    # 파이프라인 별도 엔드포인트
    path("api/ingest_news",  api_ingest_news_pipeline, name="api_ingest_news"),
    path("api/ingest_news/", api_ingest_news_pipeline),

    # RAG 유틸 / 관리
    path("api/rag/seed",      api_rag_seed,      name="api_rag_seed"),
    path("api/rag/upsert",    api_rag_upsert,    name="api_rag_upsert"),
    path("api/rag/search",    api_rag_search,    name="api_rag_search"),
    path("api/rag_search",    api_rag_search),
    path("api/rag_search/",   api_rag_search),
    path("api/rag/diag",      api_rag_diag,      name="api_rag_diag"),
    path("api/chroma/verify", api_chroma_verify, name="api_chroma_verify"),

    # 레거시 alias
    path("api/chroma_add", api_rag_upsert, name="api_chroma_add"),
    path("api/rag_query",  api_rag_search, name="api_rag_query"),

    # 단독 QA 화면
    path("rag-qa",  rag_qa_view, name="rag_qa"),
    path("rag_qa/", rag_qa_view),
    path("rag_qa", RedirectView.as_view(url="/rag_qa/", permanent=False)),

    # QARAG 대화형 엔드포인트
    path("qa-rag-chat/", qa_rag_chat, name="qa_rag_chat"),

    # ───── 피드백 (신규 + 레거시) ─────
    path("api/feedback",  api_feedback, name="api_feedback"),
    path("api/feedback/", api_feedback),
    path("api/submit_feedback",  (submit_feedback or api_feedback), name="submit_feedback"),
    path("api/submit_feedback/", (submit_feedback or api_feedback)),
    path("submit_feedback",      (submit_feedback or api_feedback), name="submit_feedback_legacy"),
    path("submit_feedback/",     (submit_feedback or api_feedback)),

    # 벡터 진단/검증
    path("api/vector_diag",   api_vector_diag,   name="api_vector_diag"),
    path("api/vector/diag",   api_vector_diag),
    path("api/vector_verify", api_vector_verify, name="api_vector_verify"),
    path("api/vector/verify", api_vector_verify),

    # 법적/크롤러 파일
    path("robots.txt", robots_txt, name="robots_txt"),

    # 🔐 법적 문서 + 가이드
    path("legal/privacy/",   legal_privacy,   name="legal_privacy"),
    path("legal/tos/",       legal_tos,       name="legal_tos"),
    path("legal/overseas/",  legal_overseas,  name="legal_overseas"),
    path("legal/tester/",    legal_tester,    name="legal_tester"),
    path("legal/bundle",     legal_bundle,    name="legal_bundle"),
    path("guide",            legal_guide,     name="legal_guide"),

    # /privacy → 정식 문서
    path("privacy",  RedirectView.as_view(url="/legal/privacy/", permanent=False), name="privacy"),
    path("privacy/", RedirectView.as_view(url="/legal/privacy/", permanent=False)),

    # 최소 버전 개인정보 페이지
    path("legal/privacy-min/", privacy_page, name="privacy_page_min"),

    # news.html 폴백 하이드레이션 JSON
    path("api/legal_bundle", api_legal_bundle, name="api_legal_bundle"),

    # /favicon.ico → /static/ragapp/favicon.ico
    path("favicon.ico", RedirectView.as_view(url=static_url("ragapp/favicon.ico"), permanent=True)),

    # 동의 레코드
    path("api/consent", consent_record, name="consent_record"),
    path("legal/consent/confirm", consent_record, name="legal_consent_confirm"),

    # (공개 데모) 이미지/표 도구
    path("media/index",  media_index_view,  name="media_index"),
    path("media/search", media_search_view, name="media_search"),
    path("table/index",  table_index_view,  name="table_index"),
    path("table/search", table_search_view, name="table_search"),

    # 업로드/도큐먼트
    path("ragadmin/upload-doc/", upload_doc_view, name="ragadmin_upload_doc"),

    # ✅ crawl-news 페이지 (ragadmin prefix 아래지만 rag_admin_site 보다 먼저 선언)
    path("ragadmin/crawl-news", RedirectView.as_view(url="/ragadmin/crawl-news/", permanent=False)),
    path("ragadmin/crawl-news/", crawl_news_view, name="ragadmin_crawl_news"),

    # ✅ Chrome DevTools 설정 파일(404 경고 제거용)
    path(".well-known/appspecific/com.chrome.devtools.json", chrome_devtools_manifest),

    # ─────────────────────────────────────────
    # LiveChat (Single include + 최소 레거시)
    # ─────────────────────────────────────────

    # ✅ livechat 앱 URL은 딱 1번만 include (namespace 고정)
    path("", include(("ragapp.livechat.urls", "livechat"), namespace="livechat")),

    # 슬래시 없는 레거시 URL → 정식 URL로 정리
    path(
        "ragadmin/live-chat",
        RedirectView.as_view(url="/ragadmin/live-chat/", permanent=False),
    ),
    path(
        "ragadmin/livechat/",
        RedirectView.as_view(url="/ragadmin/live-chat/", permanent=False),
        name="ragadmin_live_chat_legacy",
    ),

    # ── (선택) 예전 클라이언트/JS가 치는 경로만 “별칭”으로 최소 유지 ──

    # 1) QARAG 쪽 underscore 경로 (예전 호환)
    path(
        "api/live_chat/request",
        qarag_live_chat_request,
        name="qarag_live_chat_request",
    ),

    # 2) 정말로 아직 /livechat/api/request/ 를 치는 클라이언트가 있으면만 유지
    path("livechat/api/request/", livechat_views.livechat_request_api),
    path("livechat/api/end/",     livechat_views.api_livechat_end),

    # 3) 예전 콘솔이 /ragadmin/live-chat/save-session/ 로 치는 경우를 위한 별칭
    path(
        "ragadmin/live-chat/save-session/",
        livechat_views.live_chat_save_session_view,
        name="live_chat_save_session_legacy",
    ),

    # 🔽 마지막에 RAG 전용 어드민 사이트 prefix (ragadmin/)
    path("ragadmin/", rag_admin_site.urls),

    # QARAG 피드백
    path("api/qarag/feedback/", views_feedback.api_qarag_feedback, name="api_qarag_feedback"),
    path("api/qarag/feedback",  views_feedback.api_qarag_feedback),

    # PDF 분석 API
    path("api/pdf-analyze/", pdf_views.pdf_analyze_api_view, name="api_pdf_analyze"),
]

if settings.DEBUG:
    urlpatterns += djstatic(
        settings.STATIC_URL,
        document_root=str((Path(settings.BASE_DIR) / "ragapp" / "static").resolve()),
    )
    if getattr(settings, "MEDIA_URL", None) and getattr(settings, "MEDIA_ROOT", None):
        from django.conf.urls.static import static as media_static
        urlpatterns += media_static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
