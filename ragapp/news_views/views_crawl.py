# ragapp/news_views/views_crawl.py
from __future__ import annotations

import os
import json
import logging
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.http import HttpRequest, JsonResponse, HttpResponse
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from ragapp.log_utils import log_success, log_error
from ragapp.services.news_services import search_news_rss, crawl_news_bodies

# ✅ 인덱싱은 기존 shim(듀얼 vdb_upsert) 그대로 재사용
from ragapp.news_views.news_views import indexto_chroma_safe  # noqa

log = logging.getLogger(__name__)


def _ok(d: dict, status: int = 200) -> JsonResponse:
    d.setdefault("ok", True)
    return JsonResponse(d, status=status, json_dumps_params={"ensure_ascii": False})


def _fail(msg: str, *, status: int = 400, extra: dict | None = None) -> JsonResponse:
    p = {"ok": False, "error": msg}
    if extra:
        p.update(extra)
    return JsonResponse(p, status=status, json_dumps_params={"ensure_ascii": False})


def _vector_db_path() -> str:
    """
    현재 사용하는 벡터 SQLite DB 경로를 문자열로 반환.
    - settings.VECTOR_DB_PATH 우선
    - 없으면 환경변수 VECTOR_DB_PATH
    - 그래도 없으면 BASE_DIR/sqlite3/vector_store.sqlite3
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
    now = timezone.now()
    last = request.session.get(key)
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
        except Exception:
            last_dt = None
        if last_dt and (now - last_dt).total_seconds() < seconds:
            return False
    request.session[key] = now.isoformat()
    request.session.modified = True
    return True


# 설정: 메타-전용 인덱싱
_WEB_INGEST_META_ONLY = getattr(settings, "WEB_INGEST_META_ONLY", None)
if _WEB_INGEST_META_ONLY is None:
    _WEB_INGEST_META_ONLY = not bool(getattr(settings, "ALLOW_STORE_NEWS_BODY", False))


@staff_member_required
@require_http_methods(["GET"])
def crawl_news_view(request):
    ctx = {
        # page basics
        "keyword": request.GET.get("keyword", "") or "",

        # ✅ 템플릿에서 참조하는데 없으면 터질 수 있는 값들(기본값)
        "gemini_answer": "",
        "model_answer": "",
        "answer_text": "",
        "news_list": [],
        "ingest_summary": None,

        # existing info
        "VECTOR_DB_PATH": _vector_db_path(),
        "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
        "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),

        # api paths (템플릿/JS에서 쓰면 좋음)
        "WEB_API_PATH": os.environ.get("WEB_API_PATH") or getattr(settings, "WEB_API_PATH", "/api/web_qa"),
        "NEWS_INGEST_API_PATH": os.environ.get("NEWS_INGEST_API_PATH") or getattr(settings, "NEWS_INGEST_API_PATH", "/api/news_ingest/"),
    }
    return render(request, "ragadmin/crawl_news.html", ctx)


@csrf_protect
@require_http_methods(["POST"])
def api_news_ingest(request: HttpRequest) -> JsonResponse:
    """
    /api/news_ingest/ (POST, JSON)
      입력: { q: "...", answer: "..."(옵션) }
      동작: 뉴스 수집(헤더/본문) + (answer 포함) indexto_chroma_safe 업서트
    """
    if not _ratelimit(request, "rate_api_news_ingest", 5):
        return _fail("요청이 너무 잦습니다. 잠시 후 다시 시도하세요.", status=429)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST

    q = (payload.get("q") or payload.get("query") or payload.get("question") or "").strip()
    answer = (payload.get("answer") or payload.get("answer_text") or "").strip()

    if not q:
        return _fail("q(또는 query) 파라미터가 필요합니다.", status=400)

    try:
        topk = int(getattr(settings, "NEWS_TOPK", os.environ.get("NEWS_TOPK", "5")))

        news_headers = search_news_rss(q, topk)

        if _WEB_INGEST_META_ONLY:
            detailed_news_list = []
            for h in (news_headers or []):
                if isinstance(h, dict):
                    detailed_news_list.append({
                        "title": h.get("title", ""),
                        "url": h.get("url", ""),
                        "source": h.get("source", ""),
                        "published_at": h.get("published_at", ""),
                        "snippet": h.get("snippet", ""),
                        "news_body": "",
                    })
                else:
                    detailed_news_list.append({
                        "title": str(h),
                        "url": "",
                        "source": "",
                        "published_at": "",
                        "snippet": "",
                        "news_body": "",
                    })
        else:
            detailed_news_list = crawl_news_bodies(news_headers)

        ingest_summary = indexto_chroma_safe(q, answer or "", detailed_news_list)

        safe_news = [{
            "title": (n.get("title", "") if isinstance(n, dict) else str(n)),
            "url": (n.get("url", "") if isinstance(n, dict) else ""),
            "source": (n.get("source", "") if isinstance(n, dict) else ""),
            "published_at": (n.get("published_at", "") if isinstance(n, dict) else ""),
            "snippet": (n.get("snippet", "") if isinstance(n, dict) else ""),
        } for n in (detailed_news_list or [])]

        log_success(
            mode_label="crawl",
            query_text=q,
            preview="ingest ok",
            request=request,
            extra={"where": "api_news_ingest", "indexto_chroma": ingest_summary},
        )

        return _ok({"query": q, "news": safe_news, "indexto_chroma": ingest_summary})

    except Exception as e:
        log.exception("api_news_ingest 실패")
        log_error(
            mode_label="crawl",
            query_text=q,
            err_msg=str(e),
            request=request,
            extra={"where": "api_news_ingest", "stage": "exception"},
        )
        return _fail(f"뉴스 인덱싱 실패: {e}", status=500)
