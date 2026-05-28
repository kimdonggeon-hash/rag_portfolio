# ragapp/news_views/news_services.py
from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from ragapp.services import news_services as _svc

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 1) services.news_services 그대로 재노출(호환용)
#    - 여기서는 로직을 "절대" 재구현하지 않는다.
#    - FAQ 섞기/교체 규칙도 _svc 쪽이 단일 진실(Single Source of Truth)
# ─────────────────────────────────────────────────────────────

ask_gemini = _svc.ask_gemini
_embed_texts = _svc._embed_texts

fetch_article_text = _svc.fetch_article_text
crawl_news_bodies = _svc.crawl_news_bodies
search_news_rss = _svc.search_news_rss
gemini_answer_with_news = _svc.gemini_answer_with_news

indexto_chroma_safe = _svc.indexto_chroma_safe
chroma_upsert = _svc.chroma_upsert

_chunk_text = _svc._chunk_text
_slug = _svc._slug
_sha = _svc._sha
_iso = _svc._iso

# RAG (FAQ 포함 여부/교체 여부는 _svc 내부 정책을 따른다)
rag_answer_grounded = _svc.rag_answer_grounded
rag_answer_grounded_with_history = _svc.rag_answer_grounded_with_history

# 고수준 헬퍼(있으면 같이 노출)
run_rag_qa = getattr(_svc, "run_rag_qa", None)


# ─────────────────────────────────────────────────────────────
# 2) 프런트 표시용 라벨(뷰 전용 유틸은 여기 둬도 됨)
# ─────────────────────────────────────────────────────────────

def source_label(meta: Dict[str, Any]) -> str:
    title = (meta.get("title") or meta.get("url") or "문서").strip()
    src = (meta.get("source_name") or meta.get("source") or "").strip()
    u = (meta.get("url") or "").strip()

    bits: List[str] = [title]
    if src:
        bits.append(src)
    if u:
        bits.append(u)

    return " · ".join(bits)


__all__ = [
    # 생성/임베딩
    "ask_gemini",
    "_embed_texts",

    # 크롤/검색
    "fetch_article_text",
    "crawl_news_bodies",
    "search_news_rss",
    "gemini_answer_with_news",

    # 인덱싱/스토어(호환)
    "indexto_chroma_safe",
    "chroma_upsert",

    # RAG
    "rag_answer_grounded",
    "rag_answer_grounded_with_history",
    "run_rag_qa",

    # 프런트 라벨
    "source_label",

    # 헬퍼(호환)
    "_chunk_text",
    "_slug",
    "_sha",
    "_iso",
]
