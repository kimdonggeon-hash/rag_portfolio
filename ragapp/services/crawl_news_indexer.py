# ragapp/services/crawl_news_indexer.py
from __future__ import annotations

import os
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from django.conf import settings
from ragapp.services.ragchunk_audit import save_ragchunks_safe

log = logging.getLogger(__name__)


def _sha(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8", errors="ignore")).hexdigest()


def _as_bool(v: Any, default: bool = False) -> bool:
    """
    Django settings / env 값이 문자열로 들어와도 안전하게 bool 처리.
    예: "False", "0", "off" -> False
    """
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(v)


def _safe_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _first_str(data: Dict[str, Any], *keys: str) -> str:
    """
    크롤러마다 key 이름이 다를 수 있어서 여러 후보 중 첫 번째 값을 사용.
    """
    for key in keys:
        value = data.get(key)
        text = _safe_str(value)
        if text:
            return text
    return ""


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
    audit_source: str = "news",
    meta_only: bool | None = None,
) -> Dict[str, Any]:
    """
    크롤링/웹검색 결과를 벡터 DB와 RagChunk 감사 테이블에 저장.

    기본 정책:
    - 뉴스 본문 원문은 저장/인덱싱하지 않음.
    - 제목, URL, 출처, 게시일, 짧은 snippet 중심으로 인덱싱.
    - 본문 인덱싱은 아래 조건을 모두 만족할 때만 허용.
      1) ALLOW_STORE_NEWS_BODY=True
      2) WEB_INGEST_META_ONLY=False
      3) meta_only=False 또는 meta_only 인자가 None인 상태에서 설정값이 본문 허용
    """

    # 안전 기본값: 본문 저장/인덱싱 금지
    allow_body = _as_bool(getattr(settings, "ALLOW_STORE_NEWS_BODY", False), default=False)

    if meta_only is None:
        web_meta_only = _as_bool(getattr(settings, "WEB_INGEST_META_ONLY", None), default=True)
        meta_only = web_meta_only or (not allow_body)
    else:
        # 호출자가 meta_only=False를 넘겨도 ALLOW_STORE_NEWS_BODY=True가 아니면 강제로 meta_only 유지
        meta_only = _as_bool(meta_only, default=True) or (not allow_body)

    size = int(getattr(settings, "EMBED_CHUNK_SIZE", 1600))
    overlap = int(getattr(settings, "EMBED_CHUNK_OVERLAP", 200))
    min_body = int(getattr(settings, "MIN_NEWS_BODY_CHARS", 400))

    # snippet은 과도하게 길게 저장하지 않도록 제한
    snippet_limit = int(getattr(settings, "NEWS_SNIPPET_INDEX_CHARS", 500))
    snippet_limit = max(100, min(snippet_limit, 500))

    now = datetime.now(timezone.utc).isoformat()

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

    # 1) 답변 인덱싱
    answer_chunks = 0
    answer_text = _safe_str(answer)

    if answer_text:
        a_chunks = _chunk_text(answer_text, size=size, overlap=overlap)
        base_a = f"answer:{_sha(query)}"

        for i, ch in enumerate(a_chunks):
            ch_clean = _safe_str(ch)
            if not ch_clean:
                continue

            all_ids.append(f"{base_a}:{i}")
            all_docs.append(ch_clean)
            all_metas.append(
                {
                    "source": "web_answer",
                    "title": "웹검색 답변",
                    "question": _safe_str(query),
                    "ingested_at": now,
                }
            )
            answer_chunks += 1

    # 2) 뉴스 인덱싱
    news_summaries: List[Dict[str, Any]] = []

    for art in news_list or []:
        url = _first_str(art, "final_url", "url", "link")
        title = _first_str(art, "title", "headline") or "(제목 없음)"
        source_name = _first_str(art, "source", "source_name", "press", "publisher")
        published_at = _first_str(art, "published_at", "published", "date", "pub_date")
        snippet = _first_str(art, "snippet", "summary", "description")

        # 본문 key 후보는 받아두지만, 기본적으로 저장/인덱싱하지 않음
        body = _first_str(
            art,
            "news_body",
            "body",
            "content",
            "text",
            "article_body",
        )

        base_key = url or f"{title}:{source_name}:{published_at}"
        base = f"news:{_sha(base_key)}"

        safe_snippet = snippet[:snippet_limit] if snippet else ""

        # meta chunk 1개: 법적 리스크 낮은 정보만 저장
        meta_lines = [
            f"[NEWS] {title}",
            f"URL: {url}" if url else "URL: (없음)",
            f"출처: {source_name}" if source_name else "",
            f"게시: {published_at}" if published_at else "",
            f"요약: {safe_snippet}" if safe_snippet else "",
        ]

        meta_doc = "\n".join([x for x in meta_lines if x]).strip()

        chunks_for_this_news = 0
        body_cnt = 0

        if meta_doc:
            all_ids.append(f"{base}:meta")
            all_docs.append(meta_doc)
            all_metas.append(
                {
                    "source": "news",
                    "kind": "news",
                    "doc_type": "crawled_news",
                    "audit_source": audit_source,

                    "meta_only": True,
                    "url": url,
                    "title": title,
                    "source_name": source_name,
                    "published_at": published_at,
                    "ingested_at": now,
                    "query": _safe_str(query),
                    "body_stored": False,

                    # ✅ RAG 결과/근거보기 표시용
                    "display_title": title,
                    "display_source": source_name or "뉴스",
                    "display_url": url,
                }
            )
            chunks_for_this_news += 1

        # body 인덱싱은 명시적으로 허용된 경우에만 수행
        if (not meta_only) and allow_body and len(body) >= min_body:
            body_chunks = _chunk_text(body, size=size, overlap=overlap)

            for j, ch in enumerate(body_chunks):
                ch_clean = _safe_str(ch)
                if not ch_clean:
                    continue

                all_ids.append(f"{base}:body:{j}")
                all_docs.append(ch_clean)
                all_metas.append(
                    {
                        "source": "news",
                        "kind": "news",
                        "doc_type": "crawled_news",
                        "audit_source": audit_source,

                        "meta_only": False,
                        "url": url,
                        "title": title,
                        "source_name": source_name,
                        "published_at": published_at,
                        "ingested_at": now,
                        "query": _safe_str(query),
                        "body_stored": True,

                        # ✅ RAG 결과/근거보기 표시용
                        "display_title": title,
                        "display_source": source_name or "뉴스",
                        "display_url": url,
                    }
                )
                body_cnt += 1

            chunks_for_this_news += body_cnt

        news_summaries.append(
            {
                "title": title,
                "url": url,
                "chunks": chunks_for_this_news,
                "meta_only": body_cnt == 0,
                "body_chunks": body_cnt,
                "body_allowed": allow_body,
            }
        )

    # 빈 doc 제거
    rows = [
        (i, d, m)
        for (i, d, m) in zip(all_ids, all_docs, all_metas)
        if isinstance(d, str) and d.strip()
    ]

    if not rows:
        return {
            "inserted": 0,
            "indexed_count": 0,

            "answer_chunks": 0,
            "news_total_chunks": 0,
            "news_indexed_chunks": 0,
            "news_meta_chunks": 0,
            "news_body_chunks": 0,

            "news_items": news_summaries,

            "collection": getattr(settings, "VECTOR_DB_LABEL", getattr(settings, "CHROMA_COLLECTION", "")),
            "dir": _vector_db_path(),
            "source": audit_source,
            "audit_source": audit_source,

            "ingested_at": now,
            "ragchunk_saved": 0,
            "meta_only": bool(meta_only),
            "allow_body": bool(allow_body),
            "note": "인덱싱할 데이터가 없습니다.",
        }

    final_ids, final_docs, final_metas = map(list, zip(*rows))

    # 디버깅용 로그
    log.info(
        "[crawl_news_indexer] docs=%s news=%s meta_only=%s allow_body=%s body_chunks=%s meta_chunks=%s",
        len(final_docs),
        len(news_list or []),
        meta_only,
        allow_body,
        sum(1 for m in final_metas if m.get("source") == "news" and not m.get("meta_only")),
        sum(1 for m in final_metas if m.get("source") == "news" and m.get("meta_only")),
    )

    embs = _embed_texts(final_docs)
    _vup(final_ids, final_docs, final_metas, embs)

    # 3) RagChunk 감사 저장: 뉴스만 URL/제목 단위로 묶어서 저장
    from collections import defaultdict

    by_key_docs = defaultdict(list)
    by_key_meta: Dict[str, Dict[str, Any]] = {}

    for d, m in zip(final_docs, final_metas):
        if (m.get("source") or "") != "news":
            continue

        url = _safe_str(m.get("url"))
        title = _safe_str(m.get("title")) or url or "(news)"
        key = url or title

        by_key_docs[key].append(d)

        if key not in by_key_meta:
            by_key_meta[key] = {
                "kind": "news",
                "url": url,
                "title": title,
                "source_name": _safe_str(m.get("source_name")),
                "published_at": _safe_str(m.get("published_at")),
                "ingested_at": _safe_str(m.get("ingested_at")) or now,
                "meta_only": bool(m.get("meta_only")),
                "query": _safe_str(query),
                "body_stored": bool(m.get("body_stored", False)),
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

    news_meta_chunks = sum(
        1
        for m in final_metas
        if m.get("source") == "news" and bool(m.get("meta_only"))
    )

    news_body_chunks = sum(
        1
        for m in final_metas
        if m.get("source") == "news" and not bool(m.get("meta_only"))
    )

    news_total_chunks = news_meta_chunks + news_body_chunks

    return {
        # ✅ 전체 벡터DB 저장 청크 수
        "inserted": len(final_ids),

        # ✅ views_crawl.py에서 검증하기 쉽게 같은 값을 별칭으로도 제공
        "indexed_count": len(final_ids),

        # ✅ 답변/뉴스 분리 카운트
        "answer_chunks": answer_chunks,
        "news_total_chunks": news_total_chunks,
        "news_indexed_chunks": news_total_chunks,
        "news_meta_chunks": news_meta_chunks,
        "news_body_chunks": news_body_chunks,

        "news_items": news_summaries,

        # ✅ 디버깅용
        "collection": getattr(settings, "VECTOR_DB_LABEL", getattr(settings, "CHROMA_COLLECTION", "")),
        "dir": _vector_db_path(),
        "source": audit_source,
        "audit_source": audit_source,

        "ingested_at": now,
        "ragchunk_saved": ragchunk_saved,
        "meta_only": bool(meta_only),
        "allow_body": bool(allow_body),
    }