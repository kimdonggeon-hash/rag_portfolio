# ragapp/services/crawl_news_service.py
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Tuple

from django.conf import settings

from ragapp.services.crawl_news_indexer import index_answer_and_news_to_vdb

log = logging.getLogger(__name__)


def _as_bool(v: Any, default: bool = False) -> bool:
    """
    settings / env 값이 문자열로 들어와도 안전하게 bool 처리.
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


def _safe_int(v: Any, default: int, *, min_value: int = 1, max_value: int = 20) -> int:
    try:
        n = int(v)
    except Exception:
        n = default

    if n < min_value:
        return min_value
    if n > max_value:
        return max_value
    return n


def _safe_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _clamp_text(text: Any, limit: int) -> str:
    s = _safe_str(text)
    if not s:
        return ""

    s = " ".join(s.split())

    if len(s) <= limit:
        return s

    return s[:limit].strip() + "…"


def _snippet_limit() -> int:
    """
    뉴스 발췌문 저장/표시 길이 제한.
    법적 리스크 완화를 위해 기본값은 300자.
    """
    return _safe_int(
        getattr(settings, "NEWS_SNIPPET_INDEX_CHARS", 300),
        default=300,
        min_value=80,
        max_value=500,
    )


def _normalize_news_query(query: str) -> str:
    """
    사용자가 입력한 질문형 문장을 뉴스 검색 키워드로 완화.
    예:
    - "양자 컴퓨터에 대해 알려줘" -> "양자 컴퓨터"
    - "개인정보가 뭐야" -> "개인정보"
    """
    q = _safe_str(query)
    if not q:
        return ""

    # 공백 정리
    q = " ".join(q.split())

    # 끝 문장부호 제거
    q = q.rstrip("?!~.。")

    # 자주 쓰는 질문형 표현 제거
    endings = [
        "에 대해 알려줘",
        "에 대해서 알려줘",
        "에 관해 알려줘",
        "에 관해서 알려줘",
        "에 대해 설명해줘",
        "에 대해서 설명해줘",
        "에 대해 정리해줘",
        "에 대해서 정리해줘",
        "알려줘",
        "설명해줘",
        "정리해줘",
        "검색해줘",
        "찾아줘",
        "이 뭐야",
        "가 뭐야",
        "은 뭐야",
        "는 뭐야",
        "뭐야",
        "이란",
        "란",
    ]

    changed = True
    while changed:
        changed = False
        for end in endings:
            if q.endswith(end):
                q = q[: -len(end)].strip()
                changed = True

    return q or _safe_str(query)


def _resolve_meta_only(meta_only: bool | None) -> tuple[bool, bool]:
    """
    반환:
    - meta_only: True면 제목/URL/출처/snippet만 사용
    - allow_body: True면 본문 크롤링/본문 인덱싱 허용

    안전 기본값:
    - WEB_INGEST_META_ONLY=True
    - ALLOW_STORE_NEWS_BODY=False
    """
    allow_body = _as_bool(getattr(settings, "ALLOW_STORE_NEWS_BODY", False), default=False)

    if meta_only is None:
        web_meta_only = _as_bool(getattr(settings, "WEB_INGEST_META_ONLY", None), default=True)
        resolved_meta_only = web_meta_only or (not allow_body)
    else:
        # 호출자가 meta_only=False를 넘겨도 ALLOW_STORE_NEWS_BODY=True가 아니면 본문 크롤링 금지
        resolved_meta_only = _as_bool(meta_only, default=True) or (not allow_body)

    return resolved_meta_only, allow_body


def _normalize_header_item(item: Any) -> Dict[str, Any]:
    """
    RSS/search 결과를 안전한 메타 정보 형태로 통일.
    본문 원문은 여기서 저장하지 않음.
    """
    limit = _snippet_limit()

    if not isinstance(item, dict):
        return {
            "title": _safe_str(item) or "(제목 없음)",
            "url": "",
            "final_url": "",
            "source": "",
            "published_at": "",
            "snippet": "",
            "news_body": "",
        }

    url = _safe_str(item.get("final_url") or item.get("url") or item.get("link"))
    title = _safe_str(item.get("title") or item.get("headline")) or "(제목 없음)"

    snippet_raw = (
        item.get("snippet")
        or item.get("summary")
        or item.get("description")
        or ""
    )

    return {
        "title": title,
        "url": url,
        "final_url": url,
        "source": _safe_str(
            item.get("source")
            or item.get("source_name")
            or item.get("press")
            or item.get("publisher")
        ),
        "published_at": _safe_str(
            item.get("published_at")
            or item.get("published")
            or item.get("date")
            or item.get("pub_date")
        ),
        "snippet": _clamp_text(snippet_raw, limit),
        # 안전 모드에서는 본문을 비워둔다.
        "news_body": "",
    }


def _load_searchers() -> List[Tuple[str, Callable[[str, int], Any]]]:
    """
    사용 가능한 뉴스 검색 함수를 모두 로드.
    한쪽 모듈이 import 성공했지만 결과가 0건인 경우를 대비해
    news_fetcher와 news_services를 둘 다 후보로 둔다.
    """
    searchers: List[Tuple[str, Callable[[str, int], Any]]] = []

    try:
        from ragapp.services.news_fetcher import search_news_rss as s1  # type: ignore

        searchers.append(("news_fetcher.search_news_rss", s1))
    except Exception as e:
        log.warning("[crawl_news_service] news_fetcher.search_news_rss 로드 실패: %s", e)

    try:
        from ragapp.services.news_services import search_news_rss as s2  # type: ignore

        searchers.append(("news_services.search_news_rss", s2))
    except Exception as e:
        log.warning("[crawl_news_service] news_services.search_news_rss 로드 실패: %s", e)

    return searchers


def _load_crawler() -> Tuple[str, Callable[[Any], Any]] | None:
    """
    본문 크롤러 로드.
    기본 안전 모드에서는 거의 사용하지 않지만,
    allow_body=True + meta_only=False일 때만 사용.
    """
    try:
        from ragapp.services.news_fetcher import crawl_news_bodies as c1  # type: ignore

        return "news_fetcher.crawl_news_bodies", c1
    except Exception as e:
        log.warning("[crawl_news_service] news_fetcher.crawl_news_bodies 로드 실패: %s", e)

    try:
        from ragapp.services.news_services import crawl_news_bodies as c2  # type: ignore

        return "news_services.crawl_news_bodies", c2
    except Exception as e:
        log.warning("[crawl_news_service] news_services.crawl_news_bodies 로드 실패: %s", e)

    return None


def _dedupe_news_items(items: List[Any]) -> List[Any]:
    """
    URL 또는 제목 기준 중복 제거.
    """
    out: List[Any] = []
    seen: set[str] = set()

    for item in items or []:
        if not isinstance(item, dict):
            key = _safe_str(item).lower()
        else:
            url = _safe_str(item.get("final_url") or item.get("url") or item.get("link"))
            title = _safe_str(item.get("title") or item.get("headline"))
            key = (url or title).lower()

        if not key:
            key = str(len(out))

        if key in seen:
            continue

        seen.add(key)
        out.append(item)

    return out


def _search_news_headers(query: str, topk: int) -> List[Dict[str, Any]]:
    """
    여러 검색기를 순서대로 시도.
    첫 번째 검색기가 0건이어도 다음 검색기를 시도한다.
    """
    q = _safe_str(query)
    if not q:
        return []

    searchers = _load_searchers()

    if not searchers:
        log.error("[crawl_news_service] 사용 가능한 뉴스 검색기가 없습니다.")
        return []

    collected: List[Any] = []

    for name, search_fn in searchers:
        try:
            rows = search_fn(q, topk) or []
            rows = rows if isinstance(rows, list) else []

            log.info(
                "[crawl_news_service] searcher=%s query=%r rows=%s",
                name,
                q,
                len(rows),
            )

            if rows:
                collected.extend(rows)

            # 충분히 모이면 더 안 돌림
            if len(_dedupe_news_items(collected)) >= topk:
                break

        except Exception as e:
            log.exception(
                "[crawl_news_service] 뉴스 검색 실패 searcher=%s query=%r err=%s",
                name,
                q,
                e,
            )

    deduped = _dedupe_news_items(collected)
    return [_normalize_header_item(x) for x in deduped[:topk]]


def fetch_news(
    query: str,
    topk: int,
    *,
    meta_only: bool | None = None,
) -> List[Dict[str, Any]]:
    """
    뉴스 검색/크롤링.

    기본 정책:
    - 본문 크롤링하지 않음.
    - 제목, URL, 출처, 게시일, snippet만 반환.
    - 본문 크롤링은 ALLOW_STORE_NEWS_BODY=True이고 meta_only=False일 때만 수행.
    """
    resolved_meta_only, allow_body = _resolve_meta_only(meta_only)

    safe_topk = _safe_int(
        topk,
        default=int(getattr(settings, "WEB_NEWS_TOPK", 5)),
        min_value=1,
        max_value=int(getattr(settings, "WEB_NEWS_TOPK_MAX", 10)),
    )

    raw_query = _safe_str(query)
    normalized_query = _normalize_news_query(raw_query)

    # 1차: 정리된 키워드로 검색
    headers = _search_news_headers(normalized_query, safe_topk)

    # 2차: 정리된 키워드가 원문과 다르고 0건이면 원문으로도 한 번 더 검색
    if not headers and normalized_query != raw_query:
        log.info(
            "[crawl_news_service] normalized query 0건. raw query로 재시도 raw=%r normalized=%r",
            raw_query,
            normalized_query,
        )
        headers = _search_news_headers(raw_query, safe_topk)

    if not headers:
        log.warning(
            "[crawl_news_service] 뉴스 검색 결과 0건 query=%r normalized=%r topk=%s",
            raw_query,
            normalized_query,
            safe_topk,
        )
        return []

    # 안전 모드: RSS/search 메타 정보만 사용
    if resolved_meta_only or not allow_body:
        return headers

    # 본문 허용 모드: 명시적으로 허용된 경우에만 본문 크롤링
    crawler = _load_crawler()

    if not crawler:
        log.warning("[crawl_news_service] 본문 크롤러가 없어 메타 정보만 반환합니다.")
        return headers

    crawler_name, crawl_fn = crawler

    try:
        crawled = crawl_fn(headers) or []
        crawled = crawled if isinstance(crawled, list) else []

        log.info(
            "[crawl_news_service] crawler=%s headers=%s crawled=%s",
            crawler_name,
            len(headers),
            len(crawled),
        )
    except Exception as e:
        log.exception("[crawl_news_service] 본문 크롤링 실패: %s", e)
        return headers

    normalized: List[Dict[str, Any]] = []

    for item in crawled:
        if not isinstance(item, dict):
            normalized.append(_normalize_header_item(item))
            continue

        base = _normalize_header_item(item)

        body = (
            item.get("news_body")
            or item.get("body")
            or item.get("content")
            or item.get("text")
            or item.get("article_body")
            or ""
        )

        base["news_body"] = _safe_str(body)
        normalized.append(base)

    return normalized


def crawl_and_index(
    query: str,
    topk: int,
    *,
    audit_source: str = "news",
    meta_only: bool | None = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    뉴스 검색 후 벡터 DB/RagChunk에 인덱싱.

    기본적으로 본문은 저장하지 않고,
    제목/URL/출처/snippet 기반으로만 인덱싱한다.
    """
    resolved_meta_only, _allow_body = _resolve_meta_only(meta_only)

    news = fetch_news(
        query=query,
        topk=topk,
        meta_only=resolved_meta_only,
    )

    # ✅ 뉴스가 0건이면 answer 없는 빈 인덱싱을 하지 않는다.
    if not news:
        return [], {
            "inserted": 0,
            "indexed_count": 0,
            "answer_chunks": 0,
            "news_total_chunks": 0,
            "news_indexed_chunks": 0,
            "news_meta_chunks": 0,
            "news_body_chunks": 0,
            "news_items": [],
            "ragchunk_saved": 0,
            "meta_only": bool(resolved_meta_only),
            "allow_body": False,
            "source": audit_source,
            "audit_source": audit_source,
            "note": "뉴스 검색 결과가 없어 인덱싱하지 않았습니다.",
        }

    summary = index_answer_and_news_to_vdb(
        query=query,
        answer="",
        news_list=news,
        audit_source=audit_source,
        meta_only=resolved_meta_only,
    )

    return news, summary