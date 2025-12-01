# ragapp/services/ingest.py
import os
from datetime import datetime
from urllib.parse import urlparse

from django.conf import settings

from .utils import (
    chunk_text,
    slug,
    sha,
    iso,
    extract_urls_from_text,
)
from .gemini_client import embed_texts
from .chroma_store import chroma_upsert
from .news_fetcher import fetch_article_text


def _as_bool(v) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _host_allowed(url: str, allowlist: list[str]) -> bool:
    """
    ALLOWLIST_DOMAINS 규칙:
    - allowlist가 비어있으면 전부 허용
    - 도메인 완전일치 또는 서브도메인 포함(.example.com) 허용
    """
    if not url:
        return False
    if not allowlist:
        return True
    host = (urlparse(url).netloc or "").lower()
    if not host:
        return False
    for d in allowlist:
        d = d.lower()
        if host == d or host.endswith("." + d):
            return True
    return False


def indexto_chroma_safe(query: str, answer: str, news: list):
    """
    모델 답변(answer) + 뉴스 리스트(news)
    → 청크/메타청크로 자르고 → 임베딩 → 기존 컬렉션에 upsert(append)

    가드레일(중앙 규칙):
      - WEB_INGEST_TO_CHROMA=0 이면 아무것도 안 함
      - REQUIRE_SOURCE_FIELDS=1 이면 뉴스는 title/publisher/url/published_at 미충족 시 스킵
      - ALLOWLIST_DOMAINS가 비어있지 않으면 해당 도메인만 인덱싱
      - SAFE_MODE_ENABLED 또는 SAFE_SUMMARY_ONLY 켜짐 → 항상 요약/메타만 저장(원문 금지)
      - STORE_FULLTEXT=0 → 요약/메타만 저장
      - MAX_EXCERPT_CHARS=0 일 때도 무한저장 방지 위해 SAFE_MIN_BODY_CHARS를 발췌 상한으로 사용
      - Answer 내 URL 자동 크롤도 동일 규칙(allowlist/요약만 저장) 적용
    """
    # 0) 전역 차단 스위치
    if not getattr(settings, "WEB_INGEST_TO_CHROMA", True):
        return None

    # 1) 파라미터/플래그 정규화
    size = int(getattr(settings, "EMBED_CHUNK_SIZE", 1600))
    overlap = int(getattr(settings, "EMBED_CHUNK_OVERLAP", 200))
    now = datetime.utcnow().isoformat()

    require_source = _as_bool(getattr(settings, "REQUIRE_SOURCE_FIELDS", True))
    store_full = _as_bool(getattr(settings, "STORE_FULLTEXT", False))

    # 안전 모드가 켜져 있거나 요약 전용 플래그가 켜져 있으면 강제로 원문 저장 금지
    safe_mode_enabled = _as_bool(getattr(settings, "SAFE_MODE_ENABLED", True))
    safe_summary_only = _as_bool(getattr(settings, "SAFE_SUMMARY_ONLY", True))
    if safe_mode_enabled or safe_summary_only:
        store_full = False

    max_excerpt_cfg = int(getattr(settings, "MAX_EXCERPT_CHARS", 0) or 0)
    # 0이면 무제한이 되어버리니, 안전 상한선으로 SAFE_MIN_BODY_CHARS 사용
    excerpt_limit = (
        max_excerpt_cfg
        if max_excerpt_cfg > 0
        else int(getattr(settings, "SAFE_MIN_BODY_CHARS", 600) or 600)
    )

    min_chars = int(getattr(settings, "MIN_NEWS_BODY_CHARS", 400))
    allowlist = list(getattr(settings, "ALLOWLIST_DOMAINS", []) or [])

    ids, docs, metas = [], [], []

    # ── A) 모델 답변 청크 ───────────────────────────────────────
    a_chunks = chunk_text(answer or "", size=size, overlap=overlap)
    base_a = f"answer:{sha(query)}"
    for i, ch in enumerate(a_chunks):
        if not (ch or "").strip():
            continue
        ids.append(f"{base_a}:{i}")
        docs.append(ch)
        metas.append(
            {
                "source": "web_answer",
                "title": "웹검색 답변",
                "question": query,
                "ingested_at": now,
            }
        )

    # ── B) 뉴스 메타/본문/발췌 ───────────────────────────────────
    news_summaries = []
    meta_only_total = 0

    for art in (news or []):
        # 기본 필드 정규화
        url = (art.get("final_url") or art.get("url") or "").strip()
        if not _host_allowed(url, allowlist):
            # 허용 도메인 외 URL은 스킵
            continue

        host = urlparse(url).netloc if url else ""
        title = (art.get("title") or "").strip() or (host if host else "뉴스")
        publisher = (art.get("publisher") or art.get("source") or host or "").strip()
        published_at = (art.get("published_at") or "").strip()
        snippet = (art.get("snippet") or "").strip()
        body_in = (art.get("news_body") or art.get("body") or "").strip()
        excerpt_in = (art.get("excerpt") or "").strip()

        # 출처 4종 필수 검증(옵션)
        if require_source and not (title and publisher and url and published_at):
            continue

        # 안전모드/전문금지면 본문 무시(요약으로만)
        body = (body_in if store_full else "")

        base = f"news:{slug(title)}:{sha(url or title)}"

        # (1) 메타 청크(항상 저장)
        meta_doc_lines = [
            f"[META ONLY] {title}",
            f"URL: {url}",
            f"출처: {publisher}",
            f"게시: {iso(published_at)}",
            (snippet[:min(300, excerpt_limit)] if snippet else ""),
        ]
        meta_doc = "\n".join([ln for ln in meta_doc_lines if ln]).strip()

        has_full_body = bool(body and len(body) >= min_chars and store_full)

        ids.append(f"{base}:meta")
        docs.append(meta_doc)
        metas.append(
            {
                "source": "news",
                "meta_only": (not has_full_body) or (not store_full),
                "url": url,
                "title": title,
                "publisher": publisher,
                "published_at": published_at,
                "ingested_at": now,
                "is_excerpt": 1 if not store_full else 0,
            }
        )

        # (2) 본문 or 발췌
        body_cnt = 0
        if store_full and has_full_body:
            chunks = chunk_text(body, size=size, overlap=overlap)
            for j, ch in enumerate(chunks):
                if not ch.strip():
                    continue
                ids.append(f"{base}:{j}")
                docs.append(ch)
                metas.append(
                    {
                        "source": "news",
                        "url": url,
                        "title": title,
                        "publisher": publisher,
                        "published_at": published_at,
                        "ingested_at": now,
                        "is_excerpt": 0,
                    }
                )
                body_cnt += 1
        else:
            # 전문 저장 금지 또는 본문 부족 → 발췌 1청크만
            excerpt = (excerpt_in or "")
            if not excerpt:
                if body_in:
                    excerpt = body_in[:excerpt_limit]
                elif snippet:
                    excerpt = snippet[:excerpt_limit]
            if excerpt and excerpt.strip():
                ids.append(f"{base}:excerpt")
                docs.append(excerpt.strip())
                metas.append(
                    {
                        "source": "news",
                        "url": url,
                        "title": title,
                        "publisher": publisher,
                        "published_at": published_at,
                        "ingested_at": now,
                        "is_excerpt": 1,
                    }
                )
                body_cnt += 1

        if (not store_full) or (not has_full_body):
            meta_only_total += 1

        news_summaries.append(
            {
                "title": title,
                "url": url,
                "chunks": 1 + body_cnt,  # meta 1 + (body/excerpt)
                "meta_only": (not store_full) or (not has_full_body),
            }
        )

    # ── C) 답변 속 URL(Answer 링크) 동일 규칙 ───────────────────
    link_summaries = []
    link_total_chunks = 0
    if _as_bool(getattr(settings, "CRAWL_ANSWER_LINKS", True)):
        max_links = int(getattr(settings, "ANSWER_LINK_MAX", 5))
        timeout_s = int(getattr(settings, "ANSWER_LINK_TIMEOUT", 12))

        urls = extract_urls_from_text(answer)[: max(0, max_links)]
        for u in urls:
            if not _host_allowed(u, allowlist):
                # 허용 도메인 외는 크롤/저장 모두 스킵
                continue

            # fetch_article_text 내부에서 robots/레이트리밋 등을 처리하도록 설계
            body = fetch_article_text(
                u,
                timeout=timeout_s,
                min_chars=min_chars,
            )

            cnt = 0
            base = f"anslink:{slug(urlparse(u).netloc)}:{sha(u)}"

            if store_full and body:
                chunks = chunk_text(body, size=size, overlap=overlap)
                for k, ch in enumerate(chunks):
                    if not ch.strip():
                        continue
                    ids.append(f"{base}:{k}")
                    docs.append(ch)
                    metas.append(
                        {
                            "source": "answer_link",
                            "url": u,
                            "question": query,
                            "ingested_at": now,
                            "is_excerpt": 0,
                        }
                    )
                    cnt += 1
            else:
                excerpt = ""
                if body:
                    excerpt = body[:excerpt_limit]
                if excerpt.strip():
                    ids.append(f"{base}:excerpt")
                    docs.append(excerpt.strip())
                    metas.append(
                        {
                            "source": "answer_link",
                            "url": u,
                            "question": query,
                            "ingested_at": now,
                            "is_excerpt": 1,
                        }
                    )
                    cnt += 1

            link_total_chunks += cnt
            link_summaries.append({"url": u, "chunks": cnt})

    # ── D) 실제 업서트 ─────────────────────────────────────────
    clean = [(i, d, m) for i, d, m in zip(ids, docs, metas) if d and str(d).strip()]
    if not clean:
        return {
            "inserted": 0,
            "answer_chunks": 0,
            "news_total_chunks": 0,
            "news_meta_only_chunks": 0,
            "answer_link_total_chunks": 0,
            "news_items": news_summaries,
            "answer_links": link_summaries,
            "collection": settings.CHROMA_COLLECTION,
            "dir": settings.CHROMA_DB_DIR,
            "ingested_at": now,
            "note": "인덱싱할 데이터가 없습니다.",
        }

    ids, docs, metas = map(list, zip(*clean))
    embs = embed_texts(docs)
    chroma_upsert(ids=ids, docs=docs, metas=metas, embs=embs)

    ans_chunks = sum(1 for m in metas if m.get("source") == "web_answer")
    news_chunks = sum(1 for m in metas if m.get("source") == "news")

    return {
        "inserted": len(ids),
        "answer_chunks": ans_chunks,
        "news_total_chunks": news_chunks,         # meta/excerpt 포함
        "news_meta_only_chunks": meta_only_total, # 전문 저장 금지 또는 본문 부족 기사 수
        "answer_link_total_chunks": link_total_chunks,
        "news_items": news_summaries,
        "answer_links": link_summaries,
        "collection": settings.CHROMA_COLLECTION,
        "dir": settings.CHROMA_DB_DIR,
        "ingested_at": now,
    }
