# ragapp/services/crawl_news_indexer.py
from __future__ import annotations

import os
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from django.conf import settings
from ragapp.services.ragchunk_audit import save_ragchunks_safe


def _sha(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8", errors="ignore")).hexdigest()


def _vector_db_path() -> str:
    p = getattr(settings, "VECTOR_DB_PATH", None) or os.environ.get("VECTOR_DB_PATH")
    if p:
        return str(p)
    base = getattr(settings, "BASE_DIR", Path.cwd())
    return str(Path(base) / "sqlite3" / "vector_store.sqlite3")


def index_answer_and_news_to_vdb(
    *,
    query: str,
    answer: str,
    news_list: List[Dict[str, Any]],
    audit_source: str = "crawl-news",
    meta_only: bool | None = None,
) -> Dict[str, Any]:
    # meta_only 결정
    if meta_only is None:
        v = getattr(settings, "WEB_INGEST_META_ONLY", None)
        if v is None:
            v = not bool(getattr(settings, "ALLOW_STORE_NEWS_BODY", False))
        meta_only = bool(v)

    size = int(getattr(settings, "EMBED_CHUNK_SIZE", 1600))
    overlap = int(getattr(settings, "EMBED_CHUNK_OVERLAP", 200))
    min_body = int(getattr(settings, "MIN_NEWS_BODY_CHARS", 400))
    now = datetime.utcnow().isoformat()

    # chunk helper
    from ragapp.services.news_services import _chunk_text

    # embed
    try:
        from ragapp.services.vertex_embed import embed_texts as _embed_texts  # type: ignore
    except Exception:
        from ragapp.services.news_services import _embed_texts  # type: ignore

    # vdb_upsert adapter
    try:
        from ragapp.services.vdb_store import vdb_upsert as _vup  # type: ignore
    except Exception:
        from ragapp.services.vector_store import vdb_upsert as _vup  # type: ignore

    all_ids: List[str] = []
    all_docs: List[str] = []
    all_metas: List[Dict[str, Any]] = []

    # 1) 답변 인덱싱(선택)
    answer_chunks = 0
    a_chunks = _chunk_text((answer or "").strip(), size=size, overlap=overlap)
    base_a = f"answer:{_sha(query)}"
    for i, ch in enumerate(a_chunks):
        ch_clean = (ch or "").strip()
        if not ch_clean:
            continue
        all_ids.append(f"{base_a}:{i}")
        all_docs.append(ch_clean)
        all_metas.append({"source": "web_answer", "title": "웹검색 답변", "question": query, "ingested_at": now})
        answer_chunks += 1

    # 2) 뉴스 인덱싱
    news_summaries: List[Dict[str, Any]] = []

    for art in (news_list or []):
        url = (art.get("final_url") or art.get("url") or "").strip()
        title = (art.get("title") or "").strip() or "(제목 없음)"
        source_name = (art.get("source") or "").strip()
        published_at = art.get("published_at", "") or ""
        snippet = (art.get("snippet") or "").strip()
        body = (art.get("news_body") or "").strip()

        base = f"news:{_sha(url or title)}"

        # meta 1개
        meta_lines = [
            f"[NEWS] {title}",
            f"URL: {url}" if url else "URL: (없음)",
            f"출처: {source_name}" if source_name else "",
            f"게시: {published_at}" if published_at else "",
            snippet[:500] if snippet else "",
        ]
        meta_doc = "\n".join([x for x in meta_lines if x]).strip()
        if meta_doc:
            all_ids.append(f"{base}:meta")
            all_docs.append(meta_doc)
            all_metas.append({
                "source": "news",
                "meta_only": True if meta_only or (len(body) < min_body) else False,
                "url": url,
                "title": title,
                "source_name": source_name,
                "published_at": published_at,
                "ingested_at": now,
                "query": query,
            })

        chunks_for_this_news = 1

        # body (선택)
        if (not meta_only) and len(body) >= min_body:
            body_chunks = _chunk_text(body, size=size, overlap=overlap)
            body_cnt = 0
            for j, ch in enumerate(body_chunks):
                ch_clean = (ch or "").strip()
                if not ch_clean:
                    continue
                all_ids.append(f"{base}:{j}")
                all_docs.append(ch_clean)
                all_metas.append({
                    "source": "news",
                    "meta_only": False,
                    "url": url,
                    "title": title,
                    "source_name": source_name,
                    "published_at": published_at,
                    "ingested_at": now,
                    "query": query,
                })
                body_cnt += 1
            chunks_for_this_news += body_cnt

        news_summaries.append({
            "title": title,
            "url": url,
            "chunks": chunks_for_this_news,
            "meta_only": bool(meta_only) or (len(body) < min_body),
        })

    # 빈 doc 제거
    rows = [
        (i, d, m)
        for (i, d, m) in zip(all_ids, all_docs, all_metas)
        if isinstance(d, str) and d.strip()
    ]
    if not rows:
        return {
            "inserted": 0,
            "answer_chunks": 0,
            "news_total_chunks": 0,
            "news_items": news_summaries,
            "collection": getattr(settings, "VECTOR_DB_LABEL", getattr(settings, "CHROMA_COLLECTION", "")),
            "dir": _vector_db_path(),
            "ingested_at": now,
            "ragchunk_saved": 0,
            "note": "인덱싱할 데이터가 없습니다.",
        }

    final_ids, final_docs, final_metas = map(list, zip(*rows))

    embs = _embed_texts(final_docs)
    _vup(final_ids, final_docs, final_metas, embs)

    # ✅ RagChunk(감사용) 저장: 뉴스만 URL/제목 단위로 묶어서
    from collections import defaultdict
    by_key_docs = defaultdict(list)
    by_key_meta: dict[str, Dict[str, Any]] = {}

    for d, m in zip(final_docs, final_metas):
        if (m.get("source") or "") != "news":
            continue
        url = (m.get("url") or "").strip()
        title = (m.get("title") or "").strip() or url or "(news)"
        key = url or title
        by_key_docs[key].append(d)
        if key not in by_key_meta:
            by_key_meta[key] = {
                "kind": "news",
                "url": url,
                "title": title,
                "source_name": m.get("source_name", ""),
                "published_at": m.get("published_at", ""),
                "ingested_at": m.get("ingested_at", now),
                "meta_only": bool(m.get("meta_only")),
                "query": query,
            }

    ragchunk_saved = 0
    for key, docs in by_key_docs.items():
        meta0 = by_key_meta.get(key, {})
        ragchunk_saved += save_ragchunks_safe(
            texts=docs,
            title=(meta0.get("title") or key),
            url=(meta0.get("url") or ""),
            source=audit_source,
            base_meta=meta0,
        )

    news_total_chunks = sum(1 for m in final_metas if m.get("source") == "news" and not m.get("meta_only"))

    return {
        "inserted": len(final_ids),
        "answer_chunks": answer_chunks,
        "news_total_chunks": news_total_chunks,
        "news_items": news_summaries,
        "collection": getattr(settings, "VECTOR_DB_LABEL", getattr(settings, "CHROMA_COLLECTION", "")),
        "dir": _vector_db_path(),
        "ingested_at": now,
        "ragchunk_saved": ragchunk_saved,
    }
