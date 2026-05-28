# ragapp/chroma_utils.py
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import List, Dict, Any, Optional

from django.conf import settings

# ✅ 임베딩은 vertex_embed로 통일 (text-embedding-005 등 env 기반)
from ragapp.services.vertex_embed import embed_texts as _embed_texts

try:
    # 선택: vertex_embed에 current_embed_dim()이 있으면 사용
    from ragapp.services.vertex_embed import current_embed_dim as _current_embed_dim  # type: ignore
except Exception:
    _current_embed_dim = None

# ✅ Chroma는 chroma_store를 “단일 진실(SSOT)”로 사용
from ragapp.services import chroma_store as CS

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
#  임베딩 (Vertex 고정)
# ─────────────────────────────────────────────────────────────
def embed_texts(texts: List[str]) -> List[List[float]]:
    """
    텍스트 배열 → 임베딩 벡터 배열.
    - 무조건 Vertex(google-genai/Vertex 경유) 기반으로 통일
    - (중요) 로컬 SBERT 폴백 제거: 모델/차원/컬렉션 섞임 방지
    """
    texts = [t for t in (texts or []) if (t or "").strip()]
    if not texts:
        return []

    vecs = _embed_texts(texts)
    if not vecs or len(vecs) != len(texts):
        raise RuntimeError("임베딩 결과가 비어있거나 길이가 맞지 않습니다.")
    out: List[List[float]] = []
    for v in vecs:
        if not isinstance(v, (list, tuple)) or not v:
            raise RuntimeError("임베딩 벡터 형식이 예상과 다릅니다.")
        out.append([float(x) for x in v])
    return out


def _embed_dim() -> int:
    """
    현재 사용 중인 임베딩 차원 추정.
    - 1순위: vertex_embed.current_embed_dim()
    - 2순위: embed_texts(["__dim_probe__"]) 길이
    """
    if callable(_current_embed_dim):
        try:
            d = int(_current_embed_dim())
            if d > 0:
                return d
        except Exception:
            pass

    try:
        v = embed_texts(["__dim_probe__"])[0]
        return len(v)
    except Exception as e:
        log.warning("_embed_dim 추정 실패: %s", e)
        return 0


# ─────────────────────────────────────────────────────────────
#  Chroma 컬렉션/유틸 (chroma_store 래핑)
# ─────────────────────────────────────────────────────────────
def get_collection():
    """
    현재 임베딩 모델/차원에 맞는 '실제 컬렉션' 반환.
    - chroma_store가 base__<model>_<dim> 규칙으로 자동 분리
    """
    return CS.chroma_collection()


def upsert_texts(
    texts: List[str],
    metadatas: Optional[List[Dict[str, Any]]] = None,
    ids: Optional[List[str]] = None,
):
    """
    텍스트 리스트를 현재 '실제 컬렉션'에 upsert.
    - texts: 문서 본문
    - metadatas: 각 문서에 붙일 메타데이터(dict)
    - ids: 명시적 ID (없으면 UUID 자동 생성)
    """
    texts = [t for t in (texts or []) if (t or "").strip()]
    if not texts:
        return {"inserted": 0, "collection": getattr(settings, "CHROMA_COLLECTION", "")}

    if ids is None:
        ids = [str(uuid.uuid4()) for _ in texts]
    if metadatas is None:
        metadatas = [{} for _ in texts]

    if len(ids) != len(texts) or len(metadatas) != len(texts):
        raise ValueError("ids/metadatas 길이는 texts 길이와 같아야 합니다.")

    embs = embed_texts(texts)

    # chroma_store가 upsert(구버전 포함) 처리 + 컬렉션명 분리 처리
    CS.chroma_upsert(ids=ids, docs=texts, metas=metadatas, embs=embs)

    col = get_collection()
    return {
        "inserted": len(texts),
        "collection": getattr(col, "name", getattr(settings, "CHROMA_COLLECTION", "")),
    }


def count() -> int:
    """
    현재 '실제 컬렉션' 내 문서 개수.
    """
    col = get_collection()
    return CS.chroma_count(col)


def query(q: str, topk: int = 5, where=None):
    """
    쿼리 문자열 q에 대해 상위 topk 결과를 검색.

    반환 형식:
      [
        {
          "id": "...",
          "score": float(거리값),  # 낮을수록 가까움
          "meta": {...},
          "snippet": "문서 내용 일부"
        },
        ...
      ]
    """
    col = get_collection()

    res = CS.chroma_query_with_embeddings(
        col,
        query=q,
        topk=topk,
        where=where,
        include=["documents", "metadatas", "distances"],
    )

    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    ids = (res.get("ids") or [[]])[0] if "ids" in res else [""] * len(docs)

    hits = []
    for i, d in enumerate(docs):
        if not d:
            continue
        hits.append(
            {
                "id": ids[i] if i < len(ids) else "",
                "score": float(dists[i]) if (dists and i < len(dists)) else None,
                "meta": metas[i] if i < len(metas) else {},
                "snippet": (d[:500] if isinstance(d, str) else str(d)).replace("\n", " ").strip(),
            }
        )
    return hits


def seed_minimal():
    """
    Chroma 동작 확인용 최소 시드 문서 2개를 넣는 유틸.
    - upsert라 여러 번 실행해도 안전
    - 실제 컬렉션(base__model_dim)에 들어감
    """
    now = datetime.utcnow().isoformat()
    texts = [
        "이 문서는 RAG 동작 점검용 샘플입니다. 크로마 DB가 정상인지 확인하세요.",
        "두 번째 문서입니다. 간단한 검색 테스트에 사용됩니다.",
    ]
    metas = [
        {"source": "seed", "title": "doc1", "ingested_at": now},
        {"source": "seed", "title": "doc2", "ingested_at": now},
    ]
    return upsert_texts(texts, metas)
