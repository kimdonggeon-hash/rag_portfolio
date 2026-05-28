# ragapp/chroma_news_views.py
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from django.http import JsonResponse, HttpRequest
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_protect

from ragapp.services import chroma_store as CS
from ragapp import chroma_utils as CU

# ✅ 기존 news_views에서 엔드포인트를 가져와 그대로 노출(기존 유지)
from .news_views import (
    news,
    api_ping as _nested_api_ping,  # (옵션) 기존 ping과 분리하려고 남겨둠
    api_config,
    api_diag,
    api_rag_diag as _nested_api_rag_diag,  # (옵션) 기존 rag diag 유지 시 사용 가능
    api_rag_seed,
    api_search,
    web_qa_view,
    rag_qa_view,
    api_chroma_verify,
    api_news_ingest,  # ✅ 반드시 포함
)


# ─────────────────────────────────────────────────────────────────────────────
# 공용 응답 헬퍼
# ─────────────────────────────────────────────────────────────────────────────
def _ok(d: Dict[str, Any]) -> JsonResponse:
    d.setdefault("ok", True)
    return JsonResponse(d, status=200)


def _fail(message: str, extra: Dict[str, Any] | None = None) -> JsonResponse:
    payload: Dict[str, Any] = {"ok": False, "error": message}
    if extra:
        payload.update(extra)
    return JsonResponse(payload, status=200)


def _read_json(request: HttpRequest) -> Dict[str, Any]:
    try:
        return json.loads((request.body or b"").decode("utf-8") or "{}")
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# 핑 (선택)
# ─────────────────────────────────────────────────────────────────────────────
@require_http_methods(["GET"])
def api_ping(_: HttpRequest):
    # 로컬에서 간단 핑 제공 (기존 _nested_api_ping 과 별개)
    return _ok({"pong": "Pong!"})


# ─────────────────────────────────────────────────────────────────────────────
# RAG/Chroma 진단 (actual 컬렉션 기준)
# ─────────────────────────────────────────────────────────────────────────────
@require_http_methods(["GET"])
def api_rag_diag(_: HttpRequest):
    try:
        col = CS.chroma_collection()
        return _ok(
            {
                "dir": CS.settings.CHROMA_DB_DIR,
                "collection_base": CS.settings.CHROMA_COLLECTION,
                "collection_actual": getattr(col, "name", CS.settings.CHROMA_COLLECTION),
                "count": CS.chroma_count(col),
                # 선택: 환경변수/혼입 이슈 확인용
                "embed_model": getattr(CS, "_embed_model_name", lambda: "")(),
                "embed_dim": getattr(CS, "_want_embed_dim", lambda: None)(),
            }
        )
    except Exception as e:
        return _fail("진단 실패", {"exception": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# 문서 추가(업서트)
#  - ✅ CSRF 보호 활성
# ─────────────────────────────────────────────────────────────────────────────
@require_http_methods(["POST"])
@csrf_protect
def api_chroma_add(request: HttpRequest):
    payload = _read_json(request)
    if not payload:
        return _fail(
            "유효한 JSON이 아닙니다. (예: {\"texts\": [\"문서1\", \"문서2\"], \"metadatas\": [{}, {}]})"
        )

    texts: List[str] = payload.get("texts") or []
    metadatas: Optional[List[Dict[str, Any]]] = payload.get("metadatas")
    ids: Optional[List[str]] = payload.get("ids")

    if not texts or not isinstance(texts, list):
        return _fail("texts 가 비었습니다. 리스트로 보내 주세요.")

    try:
        res = CU.upsert_texts(texts, metadatas, ids)
        return _ok(res)
    except Exception as e:
        return _fail("업서트 실패", {"exception": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# 질의(검색)
#  - ✅ CSRF 보호 활성
# ─────────────────────────────────────────────────────────────────────────────
@require_http_methods(["POST"])
@csrf_protect
def api_rag_query(request: HttpRequest):
    payload = _read_json(request)
    if not payload:
        return _fail("유효한 JSON이 아닙니다. (예: {\"q\": \"질문\", \"topk\": 5})")

    q = (payload.get("q") or payload.get("query") or "").strip()
    try:
        topk = int(payload.get("topk") or 5)
    except Exception:
        topk = 5

    if not q:
        return _fail("q(query)가 비었습니다.")

    try:
        hits = CU.query(q, topk=topk)
        return _ok({"q": q, "topk": topk, "hits": hits})
    except Exception as e:
        return _fail("질의 실패", {"exception": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# 내보낼 심볼
#  - 이 모듈 하나만 urls.py에서 import 해도 기존 + 신규 엔드포인트를 다 쓸 수 있게
# ─────────────────────────────────────────────────────────────────────────────
__all__ = [
    # 신규
    "api_ping",
    "api_rag_diag",
    "api_chroma_add",
    "api_rag_query",
    # 기존(news_views에서 가져온 것들)
    "news",
    "api_config",
    "api_diag",
    "api_rag_seed",
    "api_search",
    "web_qa_view",
    "rag_qa_view",
    "api_chroma_verify",
    "api_news_ingest",
]
