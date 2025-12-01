# ragapp/views_chroma.py
from __future__ import annotations
import json
from django.http import JsonResponse, HttpRequest
from django.views.decorators.http import require_http_methods
from .chroma_utils import add_texts, query, count

def _ok(d):
    d.setdefault("ok", True)
    return JsonResponse(d, status=200, json_dumps_params={"ensure_ascii": False})

def _fail(msg, extra=None):
    p = {"ok": False, "error": msg}
    if extra:
        p.update(extra)
    # 프런트 호환 위해 200 유지
    return JsonResponse(p, status=200, json_dumps_params={"ensure_ascii": False})

# POST (CSRF 보호 활성) — 헤더로 X-CSRFToken 포함 필요
@require_http_methods(["POST"])
def api_chroma_add(request: HttpRequest):
    try:
        payload = json.loads(request.body.decode("utf-8"))
        texts = payload.get("texts") or []
        metadatas = payload.get("metadatas")
        ids = payload.get("ids")
        if not texts:
            return _fail("texts가 비었습니다. 예: {'texts':['문서1','문서2']}")
        n = add_texts(texts, metadatas, ids)
        return _ok({"inserted": n, "count": count()})
    except Exception as e:
        return _fail("추가 실패", {"reason": str(e)})

# GET 그대로 유지
@require_http_methods(["GET"])
def api_chroma_query(request: HttpRequest):
    q = (request.GET.get("q") or "").strip()
    if not q:
        return _fail("q 파라미터 필요. 예: /api/chroma/query?q=질문")
    try:
        k = int(request.GET.get("k") or 5)
    except Exception:
        k = 5
    try:
        res = query(q, topk=k)
        docs  = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        hits = [
            {
                "snippet": (docs[i] or "")[:500],
                "meta": metas[i] if i < len(metas) else {},
                "score": float(dists[i]) if dists and i < len(dists) and dists[i] is not None else None,
            }
            for i in range(len(docs))
        ]
        return _ok({"hits": hits, "count": count()})
    except Exception as e:
        return _fail("질의 실패", {"reason": str(e)})
