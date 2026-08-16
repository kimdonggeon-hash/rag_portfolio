# ragapp/news_views/views_crawl.py
from __future__ import annotations

import os
import json
import logging
from pathlib import Path
from functools import wraps
from typing import Any

from django.conf import settings
from django.http import HttpRequest, JsonResponse, HttpResponse
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods
from django.core.cache import cache

from ragapp.log_utils import log_success, log_error
from ragapp.services.utils import client_ip_for_log

# ✅ 새 안전 흐름 사용
from ragapp.services.crawl_news_service import fetch_news
from ragapp.services.crawl_news_indexer import index_answer_and_news_to_vdb

log = logging.getLogger(__name__)


def _ok(d: dict, status: int = 200) -> JsonResponse:
    d.setdefault("ok", True)
    resp = JsonResponse(d, status=status, json_dumps_params={"ensure_ascii": False})
    resp["Cache-Control"] = "no-store"
    return resp


def _fail(msg: str, *, status: int = 400, extra: dict | None = None) -> JsonResponse:
    p = {"ok": False, "error": msg}
    if extra:
        p.update(extra)

    resp = JsonResponse(p, status=status, json_dumps_params={"ensure_ascii": False})
    resp["Cache-Control"] = "no-store"
    return resp


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


def _resolve_meta_only(meta_only: bool | None = None) -> tuple[bool, bool]:
    """
    반환:
    - meta_only: True면 제목/URL/출처/snippet만 수집/인덱싱
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
        # 호출자가 meta_only=False를 보내도 ALLOW_STORE_NEWS_BODY=True가 아니면 본문 차단
        resolved_meta_only = _as_bool(meta_only, default=True) or (not allow_body)

    return resolved_meta_only, allow_body


def staff_api_required(view_func):
    """
    staff_member_required는 API에서 302 redirect가 날 수 있어서
    JSON 403 응답을 반환하는 별도 데코레이터 사용.
    """
    @wraps(view_func)
    def _wrapped(request: HttpRequest, *args, **kwargs):
        u = getattr(request, "user", None)

        if not (
            u
            and getattr(u, "is_authenticated", False)
            and getattr(u, "is_staff", False)
        ):
            return _fail("staff_only", status=403, extra={"code": "STAFF_ONLY"})

        return view_func(request, *args, **kwargs)

    return _wrapped


def _vector_db_path() -> str:
    """
    현재 사용하는 벡터 SQLite DB 경로 반환.
    """
    try:
        p = getattr(settings, "VECTOR_DB_PATH", None)
    except Exception:
        p = None

    p = p or os.environ.get("VECTOR_DB_PATH")

    if p:
        return str(p)

    base = getattr(settings, "BASE_DIR", Path.cwd())
    return str(Path(base) / "sqlite3" / "vector_store.sqlite3")


def _ratelimit(request: HttpRequest, key: str, seconds: int) -> bool:
    """
    IP + 유저ID + 세션키 기반 간단 rate limit.
    cache.add는 없을 때만 set이라 동시 요청에도 비교적 안전.
    """
    seconds = max(int(seconds or 0), 1)

    ip = (client_ip_for_log(request) or "ip").strip()
    sess = (
        getattr(getattr(request, "session", None), "session_key", None)
        or "nosess"
    ).strip()

    u = getattr(request, "user", None)
    uid = getattr(u, "id", None) or "nouser"

    bucket = f"{key}:{ip}:{uid}:{sess}"

    return bool(cache.add(bucket, "1", timeout=seconds))


@staff_member_required
@require_http_methods(["GET"])
def crawl_news_view(request: HttpRequest) -> HttpResponse:
    meta_only, allow_body = _resolve_meta_only()

    ctx = {
        # page basics
        "keyword": request.GET.get("keyword", "") or "",

        # 템플릿 기본값
        "gemini_answer": "",
        "model_answer": "",
        "answer_text": "",
        "news_list": [],
        "ingest_summary": None,

        # vector info
        "VECTOR_DB_PATH": _vector_db_path(),
        "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
        "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),

        # policy info
        "WEB_INGEST_META_ONLY": meta_only,
        "ALLOW_STORE_NEWS_BODY": allow_body,
        "NEWS_SNIPPET_INDEX_CHARS": getattr(settings, "NEWS_SNIPPET_INDEX_CHARS", 500),
        # Local/offline mode can crawl and index without calling Google OAuth/LLM.
        "CRAWL_MODEL_ENABLED": not _as_bool(
            os.environ.get("LOCAL_EMBEDDINGS"), default=False
        ),

        # api paths
        "WEB_API_PATH": (
            os.environ.get("WEB_API_PATH")
            or getattr(settings, "WEB_API_PATH", "/api/web_qa")
        ),
        "NEWS_INGEST_API_PATH": (
            os.environ.get("NEWS_INGEST_API_PATH")
            or getattr(settings, "NEWS_INGEST_API_PATH", "/api/news_ingest/")
        ),
    }

    return render(request, "ragadmin/crawl_news.html", ctx)


@staff_api_required
@csrf_protect
@require_http_methods(["POST"])
def api_news_ingest(request: HttpRequest) -> JsonResponse:
    """
    /api/news_ingest/ (POST, JSON 또는 FORM)

    입력:
    {
      "q": "검색어",
      "answer": "선택: 웹검색 답변",
      "topk": 5,
      "meta_only": true
    }

    기본 동작:
    - staff 전용
    - rate limit 적용
    - 뉴스 본문 크롤링/저장 기본 차단
    - 제목/URL/출처/게시일/snippet만 인덱싱
    """
    rate_seconds = int(getattr(settings, "NEWS_INGEST_RATE_SECONDS", 5) or 5)

    if not _ratelimit(request, "rate_api_news_ingest", rate_seconds):
        resp = _fail(
            "요청이 너무 잦습니다. 잠시 후 다시 시도하세요.",
            status=429,
            extra={"code": "RATE_LIMIT"},
        )
        resp["Retry-After"] = str(max(rate_seconds, 1))
        return resp

    # payload 파싱: JSON 우선, 실패 시 form
    payload: dict[str, Any]

    try:
        payload = json.loads((request.body or b"").decode("utf-8") or "{}")
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        try:
            payload = request.POST.dict()
        except Exception:
            payload = {}

    q = _safe_str(payload.get("q") or payload.get("query") or payload.get("question"))
    answer = _safe_str(payload.get("answer") or payload.get("answer_text"))

    if not q:
        return _fail("q(또는 query) 파라미터가 필요합니다.", status=400)

    try:
        # topk: 설정 기본값 + 상한 적용
        default_topk = _safe_int(
            getattr(settings, "NEWS_TOPK", os.environ.get("NEWS_TOPK", "5")),
            default=5,
            min_value=1,
            max_value=10,
        )

        max_topk = _safe_int(
            getattr(settings, "NEWS_TOPK_MAX", os.environ.get("NEWS_TOPK_MAX", "5")),
            default=5,
            min_value=1,
            max_value=10,
        )

        topk = _safe_int(
            payload.get("topk", default_topk),
            default=default_topk,
            min_value=1,
            max_value=max_topk,
        )

        payload_meta_only = payload.get("meta_only", None)
        resolved_meta_only, allow_body = _resolve_meta_only(payload_meta_only)

        # ✅ 새 안전 뉴스 수집 서비스 사용
        detailed_news_list = fetch_news(
            query=q,
            topk=topk,
            meta_only=resolved_meta_only,
        )

        if not detailed_news_list:
            return _fail(
                "뉴스 검색 결과가 없습니다. 외부 RSS 연결 상태를 확인한 뒤 다시 시도해 주세요.",
                status=502,
                extra={"code": "NO_NEWS", "news_count": 0},
            )

        # ✅ 새 안전 인덱서 사용
        ingest_summary = index_answer_and_news_to_vdb(
            query=q,
            answer=answer,
            news_list=detailed_news_list,
            audit_source="news",
            meta_only=resolved_meta_only,
        )

        # ✅ 인덱싱 결과 검증: 수집은 됐지만 벡터DB 저장이 0건이면 실패로 처리
        indexed_count = 0

        if isinstance(ingest_summary, dict):
            for key in (
                "inserted",
                "indexed",
                "indexed_count",
                "news_indexed_chunks",
                "news_total_chunks",
                "added",
                "added_count",
                "upserted",
                "upserted_count",
                "count",
            ):
                try:
                    indexed_count = max(indexed_count, int(ingest_summary.get(key) or 0))
                except Exception:
                    pass

        if indexed_count <= 0:
            log_error(
                mode_label="crawl",
                query_text=q,
                err_msg="뉴스 수집은 되었지만 벡터DB 인덱싱 결과가 0건입니다.",
                request=request,
                extra={
                    "where": "api_news_ingest",
                    "stage": "index_zero",
                    "ingest_summary": ingest_summary,
                    "news_count": len(detailed_news_list or []),
                    "meta_only": bool(resolved_meta_only),
                },
            )

            return _fail(
                "뉴스 수집은 되었지만 RAG 검색용 벡터DB에 저장된 항목이 없습니다.",
                status=500,
                extra={
                    "code": "INDEX_ZERO",
                    "ingest_summary": ingest_summary,
                    "news_count": len(detailed_news_list or []),
                },
            )

        # 응답에는 본문 제외
        safe_news = [
            {
                "title": _safe_str(n.get("title")) if isinstance(n, dict) else str(n),
                "url": _safe_str(
                    n.get("final_url") or n.get("url") or n.get("link")
                ) if isinstance(n, dict) else "",
                "source": _safe_str(
                    n.get("source") or n.get("source_name") or n.get("press") or n.get("publisher")
                ) if isinstance(n, dict) else "",
                "published_at": _safe_str(
                    n.get("published_at") or n.get("published") or n.get("date") or n.get("pub_date")
                ) if isinstance(n, dict) else "",
                "snippet": _safe_str(
                    n.get("snippet") or n.get("summary") or n.get("description")
                ) if isinstance(n, dict) else "",
            }
            for n in (detailed_news_list or [])
        ]

        log_success(
            mode_label="crawl",
            query_text=q,
            preview="ingest ok",
            request=request,
            extra={
                "where": "api_news_ingest",
                "ingest_summary": ingest_summary,
                "meta_only": bool(resolved_meta_only),
                "allow_body": bool(allow_body),
                "topk": topk,
                "news_count": len(safe_news),
            },
        )

        return _ok(
            {
                "query": q,
                "news": safe_news,
                "ingest_summary": ingest_summary,
                "indexto_chroma": ingest_summary,
                "meta_only": bool(resolved_meta_only),
                "allow_body": bool(allow_body),

                # ✅ 관리자 확인용 디버그 정보
                "debug": {
                    "chroma_collection": getattr(settings, "CHROMA_COLLECTION", ""),
                    "chroma_db_dir": getattr(settings, "CHROMA_DB_DIR", ""),
                    "rag_sources_filter": os.environ.get("RAG_SOURCES_FILTER", ""),
                    "audit_source": "news",
                },
            }
        )

    except Exception as e:
        log.exception("api_news_ingest 실패")

        log_error(
            mode_label="crawl",
            query_text=q,
            err_msg=str(e),
            request=request,
            extra={
                "where": "api_news_ingest",
                "stage": "exception",
            },
        )

        return _fail(f"뉴스 인덱싱 실패: {e}", status=500)
