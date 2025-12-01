# ragapp/news_views/news_views.py
from __future__ import annotations

import os
import json
import secrets
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

from django.shortcuts import render
from django.http import JsonResponse, HttpRequest, HttpResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.utils import timezone
from django.conf import settings

# 업로드 화면 전용 뷰는 분리된 모듈에서 임포트 (URL에서 이 심볼을 직접 쓰는 경우가 많아서 유지)
from .upload_views import upload_doc_view  # noqa: F401

# Feedback 모델이 없을 수 있으므로 안전 가드
try:
    from ragapp.models import ChatQueryLog, Feedback  # type: ignore
except Exception:  # pragma: no cover
    from ragapp.models import ChatQueryLog  # type: ignore
    Feedback = None  # type: ignore

from ragapp.services.safety import is_sensitive_question, safe_block_response
from ragapp.services.utils import client_ip_for_log
from ragapp.qa_data import find_best_faq_answer

# ✅ Legal 공통은 utils/legal.py 한 군데서만
from ragapp.utils.legal import validate_required_consents, build_legal_context

# 서비스 레이어
from ragapp.services.news_services import (
    gemini_answer_with_news,
    rag_answer_grounded,
    rag_answer_grounded_with_history,
)

from ragapp.log_utils import log_success, log_error

log = logging.getLogger(__name__)


def _normalize_rag_sources(raw_sources: Any) -> List[Dict[str, Any]]:
    """
    템플릿(card_rag.html)에서 바로 쓸 수 있게 rag_sources 형태 통일.
    반환:
      [{"title": "...", "url": "...", "chunk": "...", "score": 0.87}, ...]
    """
    norm: List[Dict[str, Any]] = []
    if not raw_sources:
        return norm

    for i, s in enumerate(raw_sources):
        if isinstance(s, dict):
            title = (
                s.get("title")
                or s.get("page_title")
                or s.get("file_name")
                or s.get("id")
                or f"근거 {i + 1}"
            )
            url = s.get("url") or s.get("link") or ""
            chunk = (
                s.get("chunk")
                or s.get("snippet")
                or s.get("text")
                or s.get("page_content")
                or ""
            )
            score = s.get("score") or s.get("_score") or s.get("similarity")
        else:
            title = str(s)
            url = ""
            chunk = ""
            score = None

        norm.append({"title": title, "url": url, "chunk": chunk, "score": score})

    return norm


# ─────────────────────────────────────────────
# ★ 모델 표시명은 무조건 .env에서만 읽기 (없으면 즉시 에러)
# ─────────────────────────────────────────────
def _require_env(keys: tuple[str, ...], label: str) -> str:
    for k in keys:
        v = os.environ.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    raise RuntimeError(
        f"{label} 모델명이 .env에 없습니다. 다음 키 중 하나를 .env에 설정하세요: {', '.join(keys)}"
    )


def _env_model_direct() -> str:
    return _require_env(
        (
            "GEMINI_MODEL_DIRECT",
            "GEMINI_TEXT_MODEL",
            "VERTEX_TEXT_MODEL",
            "GEMINI_MODEL",
            "GEMINI_MODEL_DEFAULT",
        ),
        label="웹/Gemini",
    )


def _env_model_rag() -> str:
    return _require_env(
        (
            "GEMINI_MODEL_RAG",
            "GEMINI_TEXT_MODEL",
            "VERTEX_TEXT_MODEL",
            "GEMINI_MODEL",
            "GEMINI_MODEL_DEFAULT",
        ),
        label="RAG",
    )


# ─────────────────────────────────────────────
# 공용 JSON 응답
# ─────────────────────────────────────────────
def _ok(d: dict) -> JsonResponse:
    d.setdefault("ok", True)
    return JsonResponse(d, status=200, json_dumps_params={"ensure_ascii": False})


def _fail(msg: str, extra: dict | None = None, status_code: int = 200) -> JsonResponse:
    p = {"ok": False, "error": msg}
    if extra:
        p.update(extra)
    return JsonResponse(p, status=status_code, json_dumps_params={"ensure_ascii": False})


# ─────────────────────────────────────────────
# 설정: 메타-전용 인덱싱
# ─────────────────────────────────────────────
_WEB_INGEST_META_ONLY = getattr(settings, "WEB_INGEST_META_ONLY", None)
if _WEB_INGEST_META_ONLY is None:
    _WEB_INGEST_META_ONLY = not bool(getattr(settings, "ALLOW_STORE_NEWS_BODY", False))


# 레이트리밋(세션)
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


def _truthy(v) -> bool:
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ("on", "1", "true", "yes", "y")


def _consent_ok_server(request: HttpRequest) -> bool:
    keys = ("consent_ok", "consent_required", "agree_privacy")
    if any(_truthy(request.POST.get(k)) for k in keys):
        return True
    if any(_truthy(request.COOKIES.get(k)) for k in keys):
        return True
    if request.session.get("consent_ok") in (True, "1", "on"):
        return True
    return False


# 어떤 형태로 리턴돼도 안전 언패킹
def _unpack_answer_sources(res) -> tuple[str, list]:
    ans = ""
    srcs: list = []
    if res is None:
        return ans, srcs
    if isinstance(res, tuple):
        if len(res) >= 1 and isinstance(res[0], str):
            ans = res[0]
        if len(res) >= 2 and isinstance(res[1], (list, tuple)):
            srcs = list(res[1])
        return ans or "", srcs or []
    if isinstance(res, dict):
        ans = str(res.get("answer", "") or res.get("text", "") or "")
        raw = res.get("sources") or res.get("headlines") or []
        if isinstance(raw, (list, tuple)):
            srcs = list(raw)
        return ans or "", srcs or []
    if isinstance(res, str):
        return res, []
    try:
        return str(res), []
    except Exception:
        return "", []


# ─────────────────────────────────────────────
# ✅ 현재 사용 중인 벡터 DB 경로 (SQLite)
# ─────────────────────────────────────────────
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


def _api_paths_ctx() -> dict:
    return {
        "WEB_API_PATH": (
            os.environ.get("WEB_API_PATH") or getattr(settings, "WEB_API_PATH", "/api/web_qa")
        ),
        "RAG_API_PATH": (
            os.environ.get("RAG_API_PATH") or getattr(settings, "RAG_API_PATH", "/api/rag_qa")
        ),
    }


def _compat_aliases_web(web_state: dict, rag_state: dict) -> dict:
    def _srcs(slist):
        out = []
        for s in (slist or []):
            if isinstance(s, dict):
                out.append({
                    "title": s.get("title", ""),
                    "url": s.get("url", ""),
                    "source": s.get("source", ""),
                    "snippet": s.get("snippet", ""),
                })
            else:
                out.append({"title": str(s), "url": "", "source": "", "snippet": ""})
        return out

    return {
        "q_gemini": web_state.get("query", ""),
        "gemini_answer": web_state.get("answer", ""),
        "gemini_error": web_state.get("error", ""),
        "news_list": _srcs(web_state.get("sources", [])),
        "q_rag": rag_state.get("query", ""),
        "rag_sources": rag_state.get("sources", []),
    }


# ─────────────────────────────────────────────
# 메인 홈 (웹/Gemini + RAG 패널)
# ─────────────────────────────────────────────
@require_http_methods(["GET", "POST"])
@ensure_csrf_cookie
def home(request: HttpRequest):
    def get_web_state():
        st = request.session.get("web_state", {})
        return {
            "query": st.get("query", ""),
            "answer": st.get("answer", ""),
            "sources": st.get("sources", []),
            "msg": st.get("msg", None),
            "error": st.get("error", None),
            "log_id": st.get("log_id", None),
        }

    def get_rag_state():
        st = request.session.get("rag_state", {})
        return {
            "query": st.get("query", ""),
            "answer": st.get("answer", ""),
            "sources": st.get("sources", []),
            "msg": st.get("msg", None),
            "error": st.get("error", None),
            "log_id": st.get("log_id", None),
        }

    def save_web_state(new_state):
        request.session["web_state"] = new_state
        request.session.modified = True

    def save_rag_state(new_state):
        request.session["rag_state"] = new_state
        request.session.modified = True

    # 첫 진입(GET, 쿼리스트링 없음)이면 세션 초기화
    if request.method == "GET" and not request.GET:
        request.session.pop("web_state", None)
        request.session.pop("rag_state", None)
        web_state = {"query": "", "answer": "", "sources": [], "msg": None, "error": None, "log_id": None}
        rag_state = {"query": "", "answer": "", "sources": [], "msg": None, "error": None, "log_id": None}
    else:
        web_state = get_web_state()
        rag_state = get_rag_state()

        if request.method == "POST":
            action = (request.POST.get("action") or request.POST.get("act") or "").strip()
            if not action:
                if (request.POST.get("query_web") or "").strip():
                    action = "web_search"
                elif (request.POST.get("query_rag") or "").strip():
                    action = "rag_search"

            # ── 웹 인덱싱 시 동의 체크 ────────────────────────
            if action == "web_ingest":
                if _consent_ok_server(request):
                    request.session["consent_ok"] = True
                    request.session.modified = True
                else:
                    ok_consent, err_consent = validate_required_consents(request)
                    if not ok_consent:
                        web_state["error"] = err_consent
                        save_web_state(web_state)
                        save_rag_state(rag_state)

                        ctx = {
                            "web_query": web_state["query"],
                            "web_answer": web_state["answer"],
                            "web_sources": web_state["sources"],
                            "web_sources_json": json.dumps(web_state["sources"], ensure_ascii=False),
                            "web_error": web_state["error"],
                            "web_msg": web_state["msg"],
                            "web_log_id": web_state.get("log_id"),
                            "rag_query": rag_state["query"],
                            "rag_answer": rag_state["answer"],
                            "rag_chunks": [],
                            "rag_error": rag_state["error"],
                            "rag_msg": rag_state["msg"],
                            "rag_sources": rag_state["sources"],
                            "rag_log_id": rag_state.get("log_id"),
                            "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
                            "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),
                            "VECTOR_DB_PATH": _vector_db_path(),
                            "model_name_gemini": _env_model_direct(),
                            "model_name_rag": _env_model_rag(),
                        }
                        ctx.update(_api_paths_ctx())
                        ctx.update(_compat_aliases_web(web_state, rag_state))
                        ctx.update(build_legal_context())
                        return render(request, "ragapp/news.html", ctx)

            # ── 웹 검색 ───────────────────────────────────────
            if action == "web_search":
                q = (request.POST.get("query_web") or "").strip()
                if not q:
                    web_state = {"query": "", "answer": "", "sources": [], "msg": None, "error": "검색어를 입력해 주세요.", "log_id": None}
                else:
                    try:
                        ans_text, headlines = _unpack_answer_sources(gemini_answer_with_news(q))
                        srcs = []
                        for h in (headlines or []):
                            try:
                                srcs.append(
                                    {
                                        "title": (h.get("title") if isinstance(h, dict) else "")
                                        or (h.get("url") if isinstance(h, dict) else "")
                                        or "(제목 없음)",
                                        "url": (h.get("url") if isinstance(h, dict) else "") or "",
                                        "snippet": (h.get("snippet") if isinstance(h, dict) else "")
                                        or (h.get("summary") if isinstance(h, dict) else ""),
                                        "source": (h.get("source") if isinstance(h, dict) else "") or "",
                                    }
                                )
                            except Exception:
                                srcs.append({"title": str(h), "url": "", "snippet": "", "source": ""})

                        log_obj = ChatQueryLog.objects.create(
                            mode="gemini",
                            question=q,
                            answer_excerpt=(ans_text or "")[:500],
                            client_ip=client_ip_for_log(request),
                            created_at=timezone.now(),
                            is_error=False,
                            error_msg="",
                            feedback="",
                            was_helpful=None,
                        )

                        web_state = {"query": q, "answer": ans_text or "", "sources": srcs, "msg": "웹 검색 완료", "error": None, "log_id": log_obj.id}
                    except Exception as e:
                        log.exception("web_search 실패")
                        err_log = ChatQueryLog.objects.create(
                            mode="gemini",
                            question=q,
                            answer_excerpt="",
                            client_ip=client_ip_for_log(request),
                            created_at=timezone.now(),
                            is_error=True,
                            error_msg=str(e),
                            feedback="",
                            was_helpful=None,
                        )
                        web_state = {"query": q, "answer": web_state.get("answer", ""), "sources": web_state.get("sources", []), "msg": None, "error": f"웹 검색 중 오류: {e}", "log_id": err_log.id}

            # ── 웹 검색 결과 인덱싱 ───────────────────────────
            elif action == "web_ingest":
                if not _ratelimit(request, "rate_web_ingest", 5):
                    web_state["error"] = "요청이 너무 잦습니다. 잠시 후 다시 시도하세요."
                    save_web_state(web_state)
                    save_rag_state(rag_state)

                    ctx = {
                        "web_query": web_state["query"],
                        "web_answer": web_state["answer"],
                        "web_sources": web_state["sources"],
                        "web_sources_json": json.dumps(web_state["sources"], ensure_ascii=False),
                        "web_error": web_state["error"],
                        "web_msg": web_state["msg"],
                        "web_log_id": web_state.get("log_id"),
                        "rag_query": rag_state["query"],
                        "rag_answer": rag_state["answer"],
                        "rag_chunks": [],
                        "rag_error": rag_state["error"],
                        "rag_msg": rag_state["msg"],
                        "rag_sources": rag_state["sources"],
                        "rag_log_id": rag_state.get("log_id"),
                        "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
                        "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),
                        "VECTOR_DB_PATH": _vector_db_path(),
                        "model_name_gemini": _env_model_direct(),
                        "model_name_rag": _env_model_rag(),
                    }
                    ctx.update(_api_paths_ctx())
                    ctx.update(_compat_aliases_web(web_state, rag_state))
                    ctx.update(build_legal_context())
                    return render(request, "ragapp/news.html", ctx)

                q = (request.POST.get("query_web") or "").strip()
                answer_payload = request.POST.get("web_answer_payload", "") or ""
                raw_sources = request.POST.get("web_sources_payload", "") or "[]"
                try:
                    src_list = json.loads(raw_sources)
                except Exception:
                    src_list = []

                try:
                    pseudo_news_list = []
                    for s in src_list:
                        if isinstance(s, dict):
                            pseudo_news_list.append(
                                {
                                    "title": s.get("title", ""),
                                    "url": s.get("url", ""),
                                    "source": s.get("source", ""),
                                    "published_at": "",
                                    "snippet": s.get("snippet", ""),
                                    "news_body": ("" if _WEB_INGEST_META_ONLY else s.get("snippet", "")),
                                }
                            )
                        else:
                            pseudo_news_list.append({"title": str(s), "url": "", "source": "", "published_at": "", "snippet": "", "news_body": ""})

                    ingest_info = indexto_chroma_safe(q, answer_payload, pseudo_news_list)
                    log.info("web_ingest 완료: %s", ingest_info)

                    web_state = {"query": q, "answer": answer_payload, "sources": src_list if isinstance(src_list, list) else [], "msg": "웹 검색 결과 인덱싱 완료", "error": None, "log_id": web_state.get("log_id")}
                except Exception as e:
                    log.exception("web_ingest 실패")
                    web_state = {"query": q, "answer": answer_payload, "sources": src_list if isinstance(src_list, list) else [], "msg": None, "error": f"웹결과 인덱싱 실패: {e}", "log_id": web_state.get("log_id")}

            # ── RAG 검색 ──────────────────────────────────────
            elif action == "rag_search":
                q = (request.POST.get("query_rag") or "").strip()
                if not q:
                    rag_state = {"query": "", "answer": "", "sources": [], "msg": None, "error": "질문을 입력해 주세요.", "log_id": None}
                else:
                    try:
                        topk = max(1, int(getattr(settings, "RAG_QUERY_TOPK", 5)))
                        fallback_topk = max(topk + 5, int(getattr(settings, "RAG_FALLBACK_TOPK", 12)))
                        max_sources = int(getattr(settings, "RAG_MAX_SOURCES", 8))

                        res = rag_answer_grounded(q, initial_topk=topk, fallback_topk=fallback_topk, max_sources=max_sources)
                        if isinstance(res, tuple) and len(res) >= 2:
                            rag_answer_text, used_hits = res[0], res[1]
                        elif isinstance(res, dict):
                            rag_answer_text = res.get("answer") or res.get("text") or ""
                            used_hits = res.get("hits") or res.get("sources") or []
                        else:
                            rag_answer_text = str(res)
                            used_hits = []

                        hits_payload: list[Dict[str, Any]] = []
                        for i, h in enumerate(used_hits or [], start=1):
                            if isinstance(h, dict):
                                meta = h.get("meta") or {}
                                title = (
                                    (meta.get("title") if isinstance(meta, dict) else None)
                                    or (meta.get("url") if isinstance(meta, dict) else None)
                                    or h.get("title")
                                    or h.get("url")
                                    or f"문서 {i}"
                                )
                                source = (
                                    (meta.get("source_name") if isinstance(meta, dict) else None)
                                    or (meta.get("source") if isinstance(meta, dict) else None)
                                    or h.get("source")
                                    or ""
                                )
                                url = (meta.get("url") if isinstance(meta, dict) else None) or h.get("url") or ""
                                snippet = h.get("snippet") or (meta.get("snippet") if isinstance(meta, dict) else None) or ""
                                score = meta.get("score") if (isinstance(meta, dict) and "score" in meta) else h.get("score")

                                hits_payload.append({"title": title, "source": source, "url": url, "snippet": snippet, "score": score})
                            else:
                                hits_payload.append({"title": str(h), "source": "", "url": "", "snippet": "", "score": None})

                        normalized_sources = _normalize_rag_sources(hits_payload)

                        log_obj = ChatQueryLog.objects.create(
                            mode="rag",
                            question=q,
                            answer_excerpt=(rag_answer_text or "")[:500],
                            client_ip=client_ip_for_log(request),
                            created_at=timezone.now(),
                            is_error=False,
                            error_msg="",
                            feedback="",
                            was_helpful=None,
                        )

                        rag_state = {"query": q, "answer": rag_answer_text or "", "sources": normalized_sources, "msg": "RAG 검색 완료", "error": None, "log_id": log_obj.id}
                    except Exception as e:
                        log.exception("rag_search 실패")
                        err_log = ChatQueryLog.objects.create(
                            mode="rag",
                            question=q,
                            answer_excerpt="",
                            client_ip=client_ip_for_log(request),
                            created_at=timezone.now(),
                            is_error=True,
                            error_msg=str(e),
                            feedback="",
                            was_helpful=None,
                        )
                        rag_state = {"query": q, "answer": rag_state.get("answer", ""), "sources": rag_state.get("sources", []), "msg": None, "error": f"RAG 검색 중 오류: {e}", "log_id": err_log.id}

            elif action == "rag_seed":
                q = (request.POST.get("query_rag") or "").strip()
                rag_state = {"query": q, "answer": rag_state.get("answer", ""), "sources": rag_state.get("sources", []), "msg": "시드 업서트 완료 (예시)", "error": None, "log_id": rag_state.get("log_id")}

            elif action == "chroma_init":
                q = (request.POST.get("query_rag") or "").strip()
                rag_state = {"query": q, "answer": rag_state.get("answer", ""), "sources": rag_state.get("sources", []), "msg": "컬렉션 초기화 완료 (예시)", "error": None, "log_id": rag_state.get("log_id")}

            else:
                if (request.POST.get("query_web") or "").strip():
                    web_state = {"query": (request.POST.get("query_web") or "").strip(), "answer": web_state.get("answer", ""), "sources": web_state.get("sources", []), "msg": None, "error": "요청을 해석할 수 없습니다. (action=web_search 폴백 실패)", "log_id": web_state.get("log_id")}
                elif (request.POST.get("query_rag") or "").strip():
                    rag_state = {"query": (request.POST.get("query_rag") or "").strip(), "answer": rag_state.get("answer", ""), "sources": rag_state.get("sources", []), "msg": None, "error": "요청을 해석할 수 없습니다. (action=rag_search 폴백 실패)", "log_id": rag_state.get("log_id")}

        save_web_state(web_state)
        save_rag_state(rag_state)

    try:
        web_sources_json = json.dumps(web_state["sources"], ensure_ascii=False)
    except Exception:
        web_sources_json = "[]"

    ctx = {
        "web_query": web_state["query"],
        "web_answer": web_state["answer"],
        "web_sources": web_state["sources"],
        "web_sources_json": web_sources_json,
        "web_error": web_state["error"],
        "web_msg": web_state["msg"],
        "web_log_id": web_state.get("log_id"),
        "rag_query": rag_state["query"],
        "rag_answer": rag_state["answer"],
        "rag_chunks": [],
        "rag_error": rag_state["error"],
        "rag_msg": rag_state["msg"],
        "rag_sources": rag_state["sources"],
        "rag_log_id": rag_state.get("log_id"),
        "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
        "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),
        "VECTOR_DB_PATH": _vector_db_path(),
        "model_name_gemini": _env_model_direct(),
        "model_name_rag": _env_model_rag(),
    }
    ctx.update(_api_paths_ctx())
    ctx.update(_compat_aliases_web(web_state, rag_state))
    ctx.update(build_legal_context())
    return render(request, "ragapp/news.html", ctx)


# ─────────────────────────────────────────────
# 예전 news 뷰 (호환용)
# ─────────────────────────────────────────────
def news(request: HttpRequest):
    if request.method == "GET" and not request.GET:
        request.session.pop("gemini_state", None)
        request.session.pop("rag_state", None)
        ctx = {
            "model_name_gemini": _env_model_direct(),
            "model_name_rag": _env_model_rag(),
            "q_gemini": "",
            "gemini_answer": "",
            "gemini_error": "",
            "news_list": [],
            "ingest_result": "",
            "ingest_error": "",
            "q_rag": "",
            "rag_answer": "",
            "rag_error": "",
            "rag_sources": [],
            "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),
            "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
            "VECTOR_DB_PATH": _vector_db_path(),
        }
        ctx.update(_api_paths_ctx())
        ctx.update(build_legal_context())
        resp = render(request, "ragapp/news.html", ctx)
        resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp["Pragma"] = "no-cache"
        resp["Expires"] = "0"
        return resp

    ctx: dict = {}
    ctx.update(_api_paths_ctx())
    ctx.update(build_legal_context())
    return render(request, "ragapp/news.html", ctx)


# ─────────────────────────────────────────────
# web_qa_view — CSRF
# ─────────────────────────────────────────────
@csrf_protect
@require_http_methods(["POST"])
def web_qa_view(request: HttpRequest):
    """
    JSON 또는 form:
      - q / query / question
    응답:
      { ok: true, answer_text: "...", answer: "...", sources: [...] }
    """
    try:
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except Exception:
            payload = request.POST

        q = (payload.get("q") or payload.get("query") or payload.get("question") or "").strip()
        if not q:
            return _fail("query가 비었습니다.", status_code=400)

        ans_text, headlines = _unpack_answer_sources(gemini_answer_with_news(q))

        log_obj = ChatQueryLog.objects.create(
            mode="gemini",
            question=q,
            answer_excerpt=(ans_text or "")[:500],
            client_ip=client_ip_for_log(request),
            created_at=timezone.now(),
            is_error=False,
            error_msg="",
            feedback="",
            was_helpful=None,
        )

        return _ok({
            "answer_text": ans_text or "",
            "answer": ans_text or "",
            "sources": headlines or [],
            "model": _env_model_direct(),
            "log_id": log_obj.id,
        })
    except Exception as e:
        log.exception("web_qa_view 실패")
        ChatQueryLog.objects.create(
            mode="gemini",
            question="(web_qa_view)",
            answer_excerpt="",
            client_ip=client_ip_for_log(request),
            created_at=timezone.now(),
            is_error=True,
            error_msg=str(e),
            feedback="",
            was_helpful=None,
        )
        return _fail(f"웹 QA 오류: {e}")


# ─────────────────────────────────────────────
# 공용: 세션 ID + 대화 로그 헬퍼 (QARAG/실시간 콘솔 공용 사용)
# ─────────────────────────────────────────────
def get_chat_session_id(request: HttpRequest) -> str:
    sid = request.session.get("chat_session_id")
    if not sid:
        sid = secrets.token_hex(16)
        request.session["chat_session_id"] = sid
        request.session.modified = True
    return sid


def log_chat_message(
    *,
    request: HttpRequest,
    session_id: str,
    channel: str,
    mode: str,
    role: str,
    message_type: str,
    question: str,
    content: str,
    answer_excerpt: str = "",
    sources: list | None = None,
    meta_extra: dict | None = None,
    is_error: bool = False,
    error_msg: str = "",
) -> ChatQueryLog:
    client_ip = client_ip_for_log(request)
    meta = dict(meta_extra or {})
    meta.setdefault("path", request.path)

    return ChatQueryLog.objects.create(
        created_at=timezone.now(),
        session_id=session_id,
        channel=channel,
        mode=mode,
        role=role,
        message_type=message_type,
        question=question,
        content=content,
        answer_excerpt=answer_excerpt,
        client_ip=client_ip,
        is_error=is_error,
        error_msg=error_msg,
        was_helpful=None,
        feedback="",
        sources=sources or [],
        meta=meta,
        legal_basis="consent",
        consent_version="",
        consent_log=None,
        legal_hold=False,
        delete_at=None,
    )


# ─────────────────────────────────────────────
# ✅ API: RAG QA (POST + CSRF)
# ─────────────────────────────────────────────
@csrf_protect
@require_http_methods(["POST"])
def rag_qa_view(request: HttpRequest):
    from django.utils.html import escape

    def _build_faq_html(q_txt: str, a_txt: str) -> str:
        q_safe = escape(q_txt or "")
        a_safe = escape(a_txt or "").replace("\n", "<br/>")
        return (
            '<div class="qarag-faq-card">'
            '  <div class="qarag-faq-card-title">🔍 자주 묻는 질문</div>'
            f'  <div class="qarag-faq-q"><strong>Q.</strong> {q_safe}</div>'
            f'  <div class="qarag-faq-card-body">{a_safe}</div>'
            "</div>"
        )

    def _serialize_log_entry(entry: ChatQueryLog) -> dict:
        return {
            "id": entry.id,
            "role": entry.role,
            "message_type": entry.message_type,
            "mode": entry.mode,
            "channel": entry.channel,
            "content": entry.content,
            "created_at": entry.created_at.isoformat(),
        }

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST

    q = (payload.get("query") or payload.get("q") or payload.get("question") or "").strip()
    if not q:
        return _fail("query가 비었습니다.", status_code=400)

    session_id = get_chat_session_id(request)

    user_log = log_chat_message(
        request=request,
        session_id=session_id,
        channel="qarag",
        mode="rag",
        role="user",
        message_type="query",
        question=q,
        content=q,
        sources=[],
        meta_extra={"where": "rag_qa_view"},
    )

    if is_sensitive_question(q):
        safe_ans = safe_block_response(q)
        user_log.mode = "blocked"
        user_log.save(update_fields=["mode"])

        answer_log = log_chat_message(
            request=request,
            session_id=session_id,
            channel="qarag",
            mode="blocked",
            role="assistant",
            message_type="answer",
            question=q,
            content=safe_ans,
            answer_excerpt=safe_ans[:500],
            sources=[],
            meta_extra={"where": "rag_qa_view", "blocked": True},
        )

        hist = request.session.get("chat_history", [])
        hist.append({"q": q, "a": safe_ans})
        request.session["chat_history"] = hist
        request.session.modified = True

        return _ok({
            "mode": "blocked",
            "model": _env_model_rag(),
            "answer_text": safe_ans,
            "answer": safe_ans,
            "answer_html": "",
            "hits": [],
            "log_id": answer_log.id,
            "session_id": session_id,
            "messages": [_serialize_log_entry(user_log), _serialize_log_entry(answer_log)],
        })

    try:
        faq_answer = find_best_faq_answer(q)
    except Exception as e:
        log.warning("find_best_faq_answer 예외: %s", e)
        faq_answer = None

    if faq_answer:
        user_log.mode = "faq"
        user_log.save(update_fields=["mode"])

        answer_log = log_chat_message(
            request=request,
            session_id=session_id,
            channel="qarag",
            mode="faq",
            role="assistant",
            message_type="answer",
            question=q,
            content=faq_answer,
            answer_excerpt=(faq_answer or "")[:500],
            sources=[],
            meta_extra={"where": "rag_qa_view", "faq": True},
        )

        log_success(mode_label="faq", query_text=q, preview="faq hit", request=request, extra={"where": "rag_qa_view", "faq": True})

        hist = request.session.get("chat_history", [])
        hist.append({"q": q, "a": faq_answer})
        request.session["chat_history"] = hist
        request.session.modified = True

        return _ok({
            "mode": "faq",
            "model": _env_model_rag(),
            "answer_text": faq_answer,
            "answer": faq_answer,
            "answer_html": _build_faq_html(q, faq_answer),
            "hits": [],
            "log_id": answer_log.id,
            "session_id": session_id,
            "messages": [_serialize_log_entry(user_log), _serialize_log_entry(answer_log)],
        })

    try:
        history_list = request.session.get("chat_history", [])

        topk = max(1, int(getattr(settings, "RAG_QUERY_TOPK", 5)))
        fallback_topk = max(topk + 5, int(getattr(settings, "RAG_FALLBACK_TOPK", 12)))
        max_sources = int(getattr(settings, "RAG_MAX_SOURCES", 8))

        res = rag_answer_grounded_with_history(
            q,
            history_list,
            base_retriever_func=rag_answer_grounded,
            initial_topk=topk,
            fallback_topk=fallback_topk,
            max_sources=max_sources,
        )

        if isinstance(res, tuple) and len(res) >= 2:
            rag_text, used_hits = res[0], res[1]
        elif isinstance(res, dict):
            rag_text = res.get("answer") or res.get("text") or ""
            used_hits = res.get("hits") or res.get("sources") or []
        else:
            rag_text = str(res)
            used_hits = []

        hits_payload = []
        for i, h in enumerate(used_hits or [], start=1):
            if isinstance(h, dict):
                m = h.get("meta") or {}
                hits_payload.append({
                    "idx": i,
                    "title": m.get("title") or m.get("url") or h.get("title") or h.get("url") or "문서",
                    "source": m.get("source_name") or m.get("source") or h.get("source") or "",
                    "url": m.get("url") or h.get("url") or "",
                    "snippet": h.get("snippet") or "",
                    "score": m.get("score") if "score" in m else h.get("score"),
                })
            else:
                hits_payload.append({"idx": i, "title": str(h), "source": "", "url": "", "snippet": "", "score": None})

        user_log.mode = "rag"
        user_log.save(update_fields=["mode"])

        answer_log = log_chat_message(
            request=request,
            session_id=session_id,
            channel="qarag",
            mode="rag",
            role="assistant",
            message_type="answer",
            question=q,
            content=rag_text,
            answer_excerpt=(rag_text or "")[:500],
            sources=hits_payload,
            meta_extra={"where": "rag_qa_view", "hit_count": len(used_hits or [])},
        )

        log_success(mode_label="rag", query_text=q, preview="rag ok (rag_qa_view)", request=request, extra={"where": "rag_qa_view", "hit_count": len(used_hits or [])})

        history_list.append({"q": q, "a": rag_text})
        request.session["chat_history"] = history_list
        request.session.modified = True

        return _ok({
            "mode": "rag",
            "model": _env_model_rag(),
            "answer_text": rag_text,
            "answer": rag_text,
            "answer_html": "",
            "hits": hits_payload,
            "log_id": answer_log.id,
            "session_id": session_id,
            "messages": [_serialize_log_entry(user_log), _serialize_log_entry(answer_log)],
        })

    except Exception as e:
        err_log = log_chat_message(
            request=request,
            session_id=session_id,
            channel="qarag",
            mode="rag",
            role="system",
            message_type="error",
            question=q,
            content="",
            sources=[],
            meta_extra={"where": "rag_qa_view", "stage": "rag_answer_grounded"},
            is_error=True,
            error_msg=str(e),
        )
        log_error(mode_label="rag", query_text=q, err_msg=str(e), request=request, extra={"where": "rag_qa_view", "stage": "rag_answer_grounded", "log_id": err_log.id})
        return _fail(f"RAG 검색 실패: {e}")


# ─────────────────────────────────────────────
# ✅ API: RAG 대화 (POST + CSRF)
# ─────────────────────────────────────────────
@csrf_protect
@require_http_methods(["POST"])
def qa_rag_chat(request: HttpRequest):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception as e:
        ChatQueryLog.objects.create(
            mode="rag",
            question="(invalid json body)",
            answer_excerpt="",
            client_ip=client_ip_for_log(request),
            created_at=timezone.now(),
            is_error=True,
            error_msg=f"invalid json: {e}",
            feedback="",
            was_helpful=None,
        )
        return JsonResponse({"ok": False, "error": f"invalid json: {e}"}, status=400, json_dumps_params={"ensure_ascii": False})

    q = (payload.get("question") or payload.get("q") or "").strip()
    if not q:
        ChatQueryLog.objects.create(
            mode="rag",
            question="(empty question)",
            answer_excerpt="",
            client_ip=client_ip_for_log(request),
            created_at=timezone.now(),
            is_error=True,
            error_msg="empty question",
            feedback="",
            was_helpful=None,
        )
        return _fail("질문이 비어 있습니다.", status_code=400)

    topk = max(1, int(getattr(settings, "RAG_QUERY_TOPK", 5)))
    fallback_topk = max(topk + 5, int(getattr(settings, "RAG_FALLBACK_TOPK", 12)))
    max_sources = int(getattr(settings, "RAG_MAX_SOURCES", 8))

    try:
        history_list = request.session.get("chat_history", [])

        res = rag_answer_grounded_with_history(
            q,
            history_list,
            base_retriever_func=rag_answer_grounded,
            initial_topk=topk,
            fallback_topk=fallback_topk,
            max_sources=max_sources,
        )

        if isinstance(res, tuple) and len(res) >= 2:
            rag_answer_text, used_hits = res[0], res[1]
        elif isinstance(res, dict):
            rag_answer_text = res.get("answer") or res.get("text") or ""
            used_hits = res.get("hits") or res.get("sources") or []
        else:
            rag_answer_text = str(res)
            used_hits = []

        hits_payload = []
        for i, h in enumerate(used_hits or [], start=1):
            if isinstance(h, dict):
                m = h.get("meta") or {}
                hits_payload.append({
                    "idx": i,
                    "title": m.get("title") or m.get("url") or h.get("title") or h.get("url") or "문서",
                    "source": m.get("source_name") or m.get("source") or h.get("source") or "",
                    "url": m.get("url") or h.get("url") or "",
                    "snippet": (h.get("snippet") or "")[:500],
                    "score": h.get("score"),
                })
            else:
                hits_payload.append({"idx": i, "title": str(h), "source": "", "url": "", "snippet": "", "score": None})

        ChatQueryLog.objects.create(
            mode="rag",
            question=q,
            answer_excerpt=(rag_answer_text or "")[:500],
            client_ip=client_ip_for_log(request),
            created_at=timezone.now(),
            is_error=False,
            error_msg="",
            feedback="",
            was_helpful=None,
        )

        log_success(mode_label="rag", query_text=q, preview="qa_rag_chat ok", request=request, extra={"where": "qa_rag_chat", "hit_count": len(hits_payload)})

        history_list.append({"q": q, "a": rag_answer_text})
        request.session["chat_history"] = history_list
        request.session.modified = True

        return _ok({"answer_text": rag_answer_text or "(빈 응답)", "answer": rag_answer_text or "(빈 응답)", "hits": hits_payload, "model": _env_model_rag()})

    except Exception as e:
        ChatQueryLog.objects.create(
            mode="rag",
            question=q,
            answer_excerpt="",
            client_ip=client_ip_for_log(request),
            created_at=timezone.now(),
            is_error=True,
            error_msg=str(e),
            feedback="",
            was_helpful=None,
        )
        log_error(mode_label="rag", query_text=q, err_msg=str(e), request=request, extra={"where": "qa_rag_chat", "stage": "rag_answer_grounded"})
        return _fail(f"RAG 오류: {e}")


def assistant_view(request: HttpRequest) -> HttpResponse:
    ctx = {"model_name_rag": _env_model_rag()}
    return render(request, "ragapp/assistant.html", ctx)


# ─────────────────────────────────────────────
# indexto_chroma_safe (로컬 shim)
# - 실제 저장은 vdb_store.vdb_upsert(듀얼: SQLite + (옵션)Chroma)를 사용
# ─────────────────────────────────────────────
def indexto_chroma_safe(query: str, answer: str, news_list: list[dict]):
    from ragapp.services.news_services import _chunk_text, _sha, _slug, _iso

    try:
        from ragapp.services.vdb_store import vdb_upsert  # type: ignore
    except Exception:
        try:
            from ragapp.services.vector_store import vdb_upsert  # type: ignore
        except Exception:
            raise RuntimeError("벡터 DB 어댑터(vdb_upsert)를 찾을 수 없습니다.")

    size = int(getattr(settings, "EMBED_CHUNK_SIZE", 1600))
    overlap = int(getattr(settings, "EMBED_CHUNK_OVERLAP", 200))
    min_body = int(getattr(settings, "MIN_NEWS_BODY_CHARS", 400))
    now = datetime.utcnow().isoformat()

    all_ids, all_docs, all_metas = [], [], []

    # 답변 텍스트 인덱싱
    a_chunks = _chunk_text(answer or "", size=size, overlap=overlap)
    base_a = f"answer:{_sha(query)}"
    for i, ch in enumerate(a_chunks):
        ch_clean = (ch or "").strip()
        if not ch_clean:
            continue
        all_ids.append(f"{base_a}:{i}")
        all_docs.append(ch_clean)
        all_metas.append({"source": "web_answer", "title": "웹검색 답변", "question": query, "ingested_at": now})

    # 뉴스 메타/본문 인덱싱
    news_summaries = []
    for art in (news_list or []):
        url = (art.get("final_url") or art.get("url") or "").strip()
        title = (art.get("title") or "").strip() or "(제목 없음)"
        body = (art.get("news_body") or "").strip()
        base = f"news:{_slug(title)}:{_sha(url or title)}"

        meta_doc_lines = [
            f"[META ONLY] {title}",
            f"URL: {url}" if url else "URL: (없음)",
            f"출처: {art.get('source','')}",
            f"게시: {_iso(art.get('published_at'))}",
            (art.get("snippet") or "")[:500],
        ]
        meta_doc = "\n".join([ln for ln in meta_doc_lines if ln]).strip()

        all_ids.append(f"{base}:meta")
        all_docs.append(meta_doc)
        all_metas.append({
            "source": "news",
            "meta_only": (len(body) < min_body) or _WEB_INGEST_META_ONLY,
            "url": url,
            "title": title,
            "source_name": art.get("source", ""),
            "published_at": art.get("published_at", ""),
            "ingested_at": now,
        })

        chunks_for_this_news = 1

        if (not _WEB_INGEST_META_ONLY) and len(body) >= min_body:
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
                    "url": url,
                    "title": title,
                    "source_name": art.get("source", ""),
                    "published_at": art.get("published_at", ""),
                    "ingested_at": now,
                })
                body_cnt += 1
            chunks_for_this_news += body_cnt

        news_summaries.append({"title": title, "url": url, "chunks": chunks_for_this_news, "meta_only": _WEB_INGEST_META_ONLY or (len(body) < min_body)})

    clean_rows = [
        (doc_id, doc_text, meta)
        for (doc_id, doc_text, meta) in zip(all_ids, all_docs, all_metas)
        if isinstance(doc_text, str) and doc_text.strip()
    ]

    if not clean_rows:
        return {
            "inserted": 0,
            "answer_chunks": 0,
            "news_total_chunks": 0,
            "news_items": news_summaries,
            "collection": getattr(settings, "VECTOR_DB_LABEL", getattr(settings, "CHROMA_COLLECTION", "")),
            "dir": _vector_db_path(),
            "ingested_at": now,
            "note": "인덱싱할 데이터가 없습니다.",
        }

    final_ids, final_docs, final_metas = map(list, zip(*clean_rows))

    try:
        from ragapp.services.vertex_embed import embed_texts as _embed_texts  # type: ignore
    except Exception:
        from ragapp.services.news_services import _embed_texts  # type: ignore

    embs = _embed_texts(final_docs)

    # ✅ vdb_store가 (옵션)Chroma + (옵션)RagChunk 미러링까지 책임
    vdb_result = vdb_upsert(final_ids, final_docs, final_metas, embs)

    ans_chunks = sum(1 for m in final_metas if m.get("source") == "web_answer")
    news_chunks = sum(1 for m in final_metas if m.get("source") == "news" and not m.get("meta_only"))

    # RagChunk 미러링 결과(설정 켜져 있으면 vdb_store에서 내려옴)
    dj = vdb_result.get("django_ragchunk") if isinstance(vdb_result, dict) else None

    return {
        "inserted": len(final_ids),
        "answer_chunks": ans_chunks,
        "news_total_chunks": news_chunks,
        "news_items": news_summaries,
        "collection": getattr(settings, "VECTOR_DB_LABEL", getattr(settings, "CHROMA_COLLECTION", "")),
        "dir": _vector_db_path(),
        "ingested_at": now,
        "vdb": vdb_result,
        "django_ragchunk": dj,
    }


# ─────────────────────────────────────────────
# ✅ QARAG → 실시간 상담 콘솔 연결 요청
# ─────────────────────────────────────────────
@csrf_protect
@require_http_methods(["POST"])
def qarag_live_chat_request(request: HttpRequest):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST

    q = (payload.get("question") or payload.get("q") or "").strip()
    client_label = (payload.get("client_label") or "").strip() or "웹 QARAG 사용자"

    room_id = f"client-{timezone.now().strftime('%Y%m%d%H%M%S')}-{os.urandom(3).hex()[:6]}"

    from ragapp.models import LiveChatRoom  # 모델 존재 전제

    room = LiveChatRoom.objects.create(
        room_id=room_id,
        client_label=client_label,
        last_question=q or "(질문 없음)",
        status="waiting",
    )

    return _ok({"room_id": room.room_id, "status": room.status})
