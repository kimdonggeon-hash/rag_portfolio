# ragapp/news_views/views_crawl.py
from __future__ import annotations

import os
import json
import logging
from datetime import datetime
from pathlib import Path
from functools import wraps

from django.conf import settings
from django.http import HttpRequest, JsonResponse, HttpResponse
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods
from django.core.cache import cache

from ragapp.log_utils import log_success, log_error
from ragapp.services.news_services import search_news_rss, crawl_news_bodies
from ragapp.services.utils import client_ip_for_log

# ✅ 인덱싱은 기존 shim(듀얼 vdb_upsert) 그대로 재사용
from ragapp.news_views.news_views import indexto_chroma_safe  # noqa

log = logging.getLogger(__name__)


def _ok(d: dict, status: int = 200) -> JsonResponse:
    d.setdefault("ok", True)
    resp = JsonResponse(d, status=status, json_dumps_params={"ensure_ascii": False})
    # API 응답은 캐시하지 않도록(디버깅/오동작 방지)
    resp["Cache-Control"] = "no-store"
    return resp


def _fail(msg: str, *, status: int = 400, extra: dict | None = None) -> JsonResponse:
    p = {"ok": False, "error": msg}
    if extra:
        p.update(extra)
    resp = JsonResponse(p, status=status, json_dumps_params={"ensure_ascii": False})
    resp["Cache-Control"] = "no-store"
    return resp


def staff_api_required(view_func):
    """
    staff_member_required는 보통 302 redirect(로그인 화면)로 흐르기 쉬워서
    API 응답으로는 403 JSON이 더 실무적이라 별도 데코레이터를 둔다.
    """
    @wraps(view_func)
    def _wrapped(request: HttpRequest, *args, **kwargs):
        u = getattr(request, "user", None)
        if not (u and getattr(u, "is_authenticated", False) and getattr(u, "is_staff", False)):
            return _fail("staff_only", status=403, extra={"code": "STAFF_ONLY"})
        return view_func(request, *args, **kwargs)
    return _wrapped


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
    """
    ✅ 실무형(원자적):
    - IP(가명처리된 형태) + 유저ID + 세션키를 섞어 cache.add로 제한
    - cache.add는 "없을 때만 set"이라 동시 요청(race)에도 비교적 안전
    """
    seconds = max(int(seconds or 0), 1)

    ip = (client_ip_for_log(request) or "ip").strip()
    sess = (getattr(getattr(request, "session", None), "session_key", None) or "nosess").strip()

    u = getattr(request, "user", None)
    uid = getattr(u, "id", None) or "nouser"

    bucket = f"{key}:{ip}:{uid}:{sess}"

    # ✅ seconds 동안 최초 1회만 통과
    return bool(cache.add(bucket, "1", timeout=seconds))


# 설정: 메타-전용 인덱싱
_WEB_INGEST_META_ONLY = getattr(settings, "WEB_INGEST_META_ONLY", None)
if _WEB_INGEST_META_ONLY is None:
    _WEB_INGEST_META_ONLY = not bool(getattr(settings, "ALLOW_STORE_NEWS_BODY", False))


@staff_member_required
@require_http_methods(["GET"])
def crawl_news_view(request: HttpRequest) -> HttpResponse:
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


@staff_api_required
@csrf_protect
@require_http_methods(["POST"])
def api_news_ingest(request: HttpRequest) -> JsonResponse:
    """
    /api/news_ingest/ (POST, JSON)
      입력: { q: "...", answer: "..."(옵션) }
      동작: 뉴스 수집(헤더/본문) + (answer 포함) indexto_chroma_safe 업서트

    ✅ 유지 + 개선
    - staff 전용 (시크릿/비로그인 호출 차단)
    - ratelimit을 cache.add 기반으로 강화(동시 요청에 강함)
    - topk 상한(기본 5)으로 비용/폭주 방지
    - JSON/FORM 모두 안전 파싱
    """
    rate_seconds = int(getattr(settings, "NEWS_INGEST_RATE_SECONDS", 5) or 5)
    if not _ratelimit(request, "rate_api_news_ingest", rate_seconds):
        resp = _fail("요청이 너무 잦습니다. 잠시 후 다시 시도하세요.", status=429, extra={"code": "RATE_LIMIT"})
        resp["Retry-After"] = str(max(rate_seconds, 1))
        return resp

    # payload 파싱(JSON 우선, 실패 시 form)
    payload: dict
    try:
        payload = json.loads((request.body or b"").decode("utf-8") or "{}")
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        try:
            payload = request.POST.dict()
        except Exception:
            payload = {}

    q = (payload.get("q") or payload.get("query") or payload.get("question") or "").strip()
    answer = (payload.get("answer") or payload.get("answer_text") or "").strip()

    if not q:
        return _fail("q(또는 query) 파라미터가 필요합니다.", status=400)

    try:
        # ✅ 비용/폭주 방지: topk 상한 고정(정책이 5개면 여기서 강제)
        topk_raw = getattr(settings, "NEWS_TOPK", os.environ.get("NEWS_TOPK", "5"))
        try:
            topk = int(topk_raw)
        except Exception:
            topk = 5
        topk = max(1, min(topk, 5))

        news_headers = search_news_rss(q, topk)

        if _WEB_INGEST_META_ONLY:
            detailed_news_list = []
            for h in (news_headers or []):
                if isinstance(h, dict):
                    detailed_news_list.append(
                        {
                            "title": h.get("title", ""),
                            "url": h.get("url", ""),
                            "source": h.get("source", ""),
                            "published_at": h.get("published_at", ""),
                            "snippet": h.get("snippet", ""),
                            "news_body": "",
                        }
                    )
                else:
                    detailed_news_list.append(
                        {
                            "title": str(h),
                            "url": "",
                            "source": "",
                            "published_at": "",
                            "snippet": "",
                            "news_body": "",
                        }
                    )
        else:
            detailed_news_list = crawl_news_bodies(news_headers)

        ingest_summary = indexto_chroma_safe(q, answer or "", detailed_news_list)

        # 응답에는 본문 제외(프론트 표시/로그/노출 최소화)
        safe_news = [
            {
                "title": (n.get("title", "") if isinstance(n, dict) else str(n)),
                "url": (n.get("url", "") if isinstance(n, dict) else ""),
                "source": (n.get("source", "") if isinstance(n, dict) else ""),
                "published_at": (n.get("published_at", "") if isinstance(n, dict) else ""),
                "snippet": (n.get("snippet", "") if isinstance(n, dict) else ""),
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
                "indexto_chroma": ingest_summary,
                "meta_only": bool(_WEB_INGEST_META_ONLY),
                "topk": topk,
                "news_count": len(safe_news),
            },
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
