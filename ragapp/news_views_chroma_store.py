# ragapp/views_chroma_store.py
from __future__ import annotations
import json
from typing import Any, Dict, List, Optional

from django.http import JsonResponse, HttpRequest
from django.views.decorators.http import require_http_methods

# Chroma 스토리지 래퍼
from . import chroma_storage as store


# ──────────────────────────────────────────────
# 공용 응답 헬퍼
# ──────────────────────────────────────────────
def _ok(d: Dict[str, Any]) -> JsonResponse:
    d.setdefault("ok", True)
    return JsonResponse(d, status=200, json_dumps_params={"ensure_ascii": False})

def _fail(msg: str, extra: Optional[Dict[str, Any]] = None) -> JsonResponse:
    p = {"ok": False, "error": msg}
    if extra:
        p.update(extra)
    # 기존 프런트 호환 위해 200 유지
    return JsonResponse(p, status=200, json_dumps_params={"ensure_ascii": False})


# ──────────────────────────────────────────────
# 핑
# ──────────────────────────────────────────────
@require_http_methods(["GET"])
def api_ping(_: HttpRequest):
    return _ok({"pong": "Pong!"})


# ──────────────────────────────────────────────
# 초기화: 폴더/컬렉션 보장
# (POST, CSRF 보호 활성)
# ──────────────────────────────────────────────
@require_http_methods(["POST"])
def api_chroma_init(_: HttpRequest):
    try:
        d, c, n = store.init_chroma()
        return _ok({"dir": d, "collection": c, "count": n})
    except Exception as e:
        return _fail("init failed", {"reason": str(e)})


# ──────────────────────────────────────────────
# 저장(업서트)
# body:
#  - (1) {"documents":[...], "metadatas":[...], "ids":[...]}  # 여러개
#  - (2) {"text":"...", "metadata":{...}, "id":"..."}         # 하나
# (POST, CSRF 보호 활성)
# ──────────────────────────────────────────────
@require_http_methods(["POST"])
def api_chroma_put(request: HttpRequest):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = {}

    if "documents" in payload:
        docs: List[str] = payload.get("documents") or []
        metas: Optional[List[Dict[str, Any]]] = payload.get("metadatas") or [{} for _ in docs]
        ids: Optional[List[str]] = payload.get("ids")
        if not isinstance(docs, list) or not docs:
            return _fail("documents 리스트가 비었습니다.")
    else:
        text = payload.get("text") or payload.get("document")
        if not text:
            return _fail("documents 또는 text 필드를 제공하세요.")
        docs = [text]
        metas = [payload.get("metadata") or {}]
        ids = [payload.get("id")] if payload.get("id") else None

    try:
        res = store.upsert_texts(docs, metadatas=metas, ids=ids)
        return _ok(res)
    except Exception as e:
        return _fail("upsert failed", {"reason": str(e)})


# ──────────────────────────────────────────────
# 조회
# body: {"query":"...", "topk":5}
# (POST, CSRF 보호 활성)
# ──────────────────────────────────────────────
@require_http_methods(["POST"])
def api_chroma_query(request: HttpRequest):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = {}

    q = (payload.get("query") or payload.get("q") or "").strip()
    try:
        topk = int(payload.get("topk") or 5)
    except Exception:
        topk = 5

    if not q:
        return _fail("query가 비었습니다.")

    try:
        res = store.query(q, topk=topk)
        # 보기 좋게 snippet 구성
        docs  = res.get("documents") or []
        metas = res.get("metadatas") or []
        ids   = res.get("ids") or []
        dists = res.get("distances") or []

        items: List[Dict[str, Any]] = []
        limit = min(len(docs), topk)
        for i in range(limit):
            snip = (docs[i] or "")[:500].replace("\n", " ")
            items.append({
                "id": ids[i] if i < len(ids) else "",
                "distance": float(dists[i]) if (i < len(dists) and dists[i] is not None) else None,
                "snippet": snip,
                "meta": metas[i] if i < len(metas) else {},
            })

        return _ok({
            "items": items,
            "dir": res.get("dir"),
            "collection": res.get("collection"),
            "topk": res.get("topk", topk),
        })
    except Exception as e:
        return _fail("query failed", {"reason": str(e)})


# ──────────────────────────────────────────────
# 카운트
# ──────────────────────────────────────────────
@require_http_methods(["GET"])
def api_chroma_count(_: HttpRequest):
    try:
        n = store.count()
        d, c, _ = store.init_chroma()
        return _ok({"count": n, "dir": d, "collection": c})
    except Exception as e:
        return _fail("count failed", {"reason": str(e)})


__all__ = [
    "api_ping",
    "api_chroma_init",
    "api_chroma_put",
    "api_chroma_query",
    "api_chroma_count",
]
