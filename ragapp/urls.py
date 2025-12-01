# ragapp/urls.py
from django.urls import path

# 화면 / 사용자용 뷰
from ragapp.news_views.news_views import (
    home,
    news,              # 레거시 데모 화면(필요시)
    rag_qa_view,
    qa_rag_chat,       # QARAG 챗 API (POST)
    submit_feedback,   # 👍👎 피드백 저장
    assistant_view,    # 엔드유저용 심플 챗 화면
    api_news_ingest,   # 원격 뉴스 인덱싱
)

# API / 내부 진단용 뷰
from ragapp.news_views.api_views import (
    api_ping,
    api_config,
    api_diag,
    api_chroma_verify,
    api_rag_seed,
    api_rag_diag,
    api_rag_upsert,
    api_rag_search,
)

app_name = "ragapp"

urlpatterns = [
    # 메인/화면
    path("", home, name="home"),
    path("news/", news, name="news"),  # 레거시 데모
    path("assistant/", assistant_view, name="assistant_view"),

    # 웹 QA & 뉴스 인덱싱
    path("api/news_ingest", api_news_ingest, name="api_news_ingest"),

    # RAG 대화/QA
    path("rag-qa", rag_qa_view, name="rag_qa"),
    path("qa-rag-chat/", qa_rag_chat, name="qa_rag_chat"),

    # 피드백 저장
    path("api/feedback", submit_feedback, name="submit_feedback"),

    # 진단/상태 & RAG 유틸
    path("api/ping", api_ping, name="api_ping"),
    path("api/config", api_config, name="api_config"),
    path("api/diag", api_diag, name="api_diag"),
    path("api/rag/seed", api_rag_seed, name="api_rag_seed"),
    path("api/rag/diag", api_rag_diag, name="api_rag_diag"),
    path("api/rag/upsert", api_rag_upsert, name="api_rag_upsert"),
    path("api/rag/search", api_rag_search, name="api_rag_search"),
    path("api/chroma/verify", api_chroma_verify, name="api_chroma_verify"),
]
