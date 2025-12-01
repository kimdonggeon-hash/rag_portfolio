# ragapp/news_views.py
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.http import JsonResponse, HttpRequest
from django.views.decorators.http import require_http_methods

# nested views에서 일부 엔드포인트를 가져와 노출 (기존 유지)
from .news_views import (
    news,
    api_ping as _nested_api_ping,  # 로컬 정의 api_ping이 아래에 있으므로 이름 충돌 방지
    api_config, api_diag,
    api_rag_diag as _nested_api_rag_diag,
    api_rag_seed,
    api_search, web_qa_view, rag_qa_view,
    api_chroma_verify,
    api_news_ingest,   # ✅ 반드시 포함
)

# 크로마 유틸 (Step 4에서 만든 파일)
from ragapp import chroma_utils as CU


# ─────────────────────────────────────────────────────────────────────────────
# 공용 응답 헬퍼
# ─────────────────────────────────────────────────────────────────────────────
def _ok(d: Dict[str, Any]) -> JsonResponse:
    d.setdefault("ok", True)
    return JsonResponse(d, status=200)

def _fail(message: str, extra: Dict[str, Any] | None = None) -> JsonResponse:
    payload = {"ok": False, "error": message}
    if extra:
        payload.update(extra)
    return JsonResponse(payload, status=200)


# ─────────────────────────────────────────────────────────────────────────────
# 핑 (선택)
# ─────────────────────────────────────────────────────────────────────────────
@require_http_methods(["GET"])
def api_ping(_: HttpRequest):
    # 로컬에서 간단 핑 제공 (중첩 모듈의 api_ping과 동작은 동일; 라우팅 편의용)
    return _ok({"pong": "Pong!"})


# ─────────────────────────────────────────────────────────────────────────────
# RAG/Chroma 진단
# ─────────────────────────────────────────────────────────────────────────────
@require_http_methods(["GET"])
def api_rag_diag(_: HttpRequest):
    try:
        return _ok({
            "dir": CU.settings.CHROMA_DB_DIR,
            "collection": CU.settings.CHROMA_COLLECTION,
            "count": CU.count(),
        })
    except Exception as e:
        return _fail("진단 실패", {"exception": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# 문서 추가(업서트)
#  - CSRF 보호 활성: 프런트/클라이언트는 X-CSRFToken 헤더를 포함해야 함.
# ─────────────────────────────────────────────────────────────────────────────
@require_http_methods(["POST"])
def api_chroma_add(request: HttpRequest):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return _fail("유효한 JSON이 아닙니다. (예: {\"texts\": [\"문서1\", \"문서2\"], \"metadatas\": [{}, {}]})")

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
#  - CSRF 보호 활성: 프런트/클라이언트는 X-CSRFToken 헤더를 포함해야 함.
# ─────────────────────────────────────────────────────────────────────────────
@require_http_methods(["POST"])
def api_rag_query(request: HttpRequest):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
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
# ─────────────────────────────────────────────────────────────────────────────
__all__ = [
    "api_ping",
    "api_rag_diag",
    "api_chroma_add",
    "api_rag_query",
]
