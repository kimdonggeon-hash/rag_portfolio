# ragapp/services/crawl_news_service.py
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from django.conf import settings

from ragapp.services.crawl_news_indexer import index_news_to_vdb


def fetch_news(query: str, topk: int, *, meta_only: bool | None = None) -> List[Dict[str, Any]]:
    # meta_only 결정
    if meta_only is None:
        v = getattr(settings, "WEB_INGEST_META_ONLY", None)
        if v is None:
            v = not bool(getattr(settings, "ALLOW_STORE_NEWS_BODY", False))
        meta_only = bool(v)

    # fetcher 우선, 없으면 services 폴백
    try:
        from ragapp.services.news_fetcher import search_news_rss as _search
        from ragapp.services.news_fetcher import crawl_news_bodies as _crawl
    except Exception:
        from ragapp.services.news_services import search_news_rss as _search
        from ragapp.services.news_services import crawl_news_bodies as _crawl

    headers = _search(query, topk) or []

    if meta_only:
        out: List[Dict[str, Any]] = []
        for h in headers:
            if isinstance(h, dict):
                out.append({
                    "title": h.get("title", ""),
                    "url": h.get("url", ""),
                    "source": h.get("source", ""),
                    "published_at": h.get("published_at", ""),
                    "snippet": h.get("snippet", ""),
                    "news_body": "",
                })
            else:
                out.append({"title": str(h), "url": "", "source": "", "published_at": "", "snippet": "", "news_body": ""})
        return out

    return _crawl(headers) or []


def crawl_and_index(
    query: str,
    topk: int,
    *,
    audit_source: str = "crawl-news",
    meta_only: bool | None = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    news = fetch_news(query, topk, meta_only=meta_only)
    summary = index_news_to_vdb(query=query, news_list=news, audit_source=audit_source, meta_only=meta_only)
    return news, summary
