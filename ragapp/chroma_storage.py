# ragapp/services/chroma_store.py
from __future__ import annotations
import hashlib
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from django.db import transaction
from django.db.models import QuerySet

from ragapp.models import RagChunk

# =========[ 유틸 ]===========================================================

def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()

def _to_bytes(vec: Sequence[float]) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()

def _from_bytes(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32)

def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))

# =========[ 임베딩 함수: 자동 탐색 → 실패 시 친절 에러 ]======================
def embed_texts(texts: List[str]) -> List[List[float]]:
    """
    프로젝트 내 임베딩 함수를 자동 탐색해서 호출.
    - 우선순위:
      1) ragapp.services.embeddings.embed_texts
      2) ragapp.services.rag_engine.embed_texts
      3) ragapp.services.news_services.embed_texts
    - 위가 없으면 명시적으로 에러를 내어 경로 안내.
    """
    for modpath in (
        "ragapp.services.embeddings",
        "ragapp.services.rag_engine",
        "ragapp.news_views.news_services",
        "ragapp.services.news_services",
    ):
        try:
            mod = __import__(modpath, fromlist=["*"])
            fn = getattr(mod, "embed_texts", None)
            if callable(fn):
                return fn(texts)
        except Exception:
            pass
    raise RuntimeError(
        "embed_texts() 연결 대상이 없습니다. "
        "다음 중 하나에 embed_texts(texts: List[str]) -> List[List[float]]를 구현하세요:\n"
        " - ragapp/services/embeddings.py\n"
        " - ragapp/services/rag_engine.py\n"
        " - ragapp/news_views/news_services.py\n"
        "또는 add(..., embeddings=...)로 벡터를 직접 전달하세요."
    )

# =========[ 드롭-인 컬렉션 ]==================================================
class SQLiteCollection:
    """
    Chroma Collection과 유사 인터페이스:
      - add(ids, documents, metadatas, embeddings=None)
      - query(query_texts, n_results=5, where=None, embeddings=None)
    반환 구조 또한 Chroma 스타일(ids/documents/metadatas/distances).
    """
    name: str = "sqlite_collection"

    def add(
        self,
        ids: Optional[List[str]] = None,
        documents: Optional[List[str]] = None,
        metadatas: Optional[List[Dict[str, Any]]] = None,
        embeddings: Optional[List[List[float]]] = None,
    ) -> None:
        documents = documents or []
        metadatas = metadatas or [{} for _ in documents]
        if embeddings is None:
            embeddings = embed_texts(documents)
        if len(embeddings) != len(documents):
            raise ValueError("embeddings 개수와 documents 개수가 다릅니다.")

        to_create: List[RagChunk] = []
        with transaction.atomic():
            for i, text in enumerate(documents):
                md = metadatas[i] or {}
                url = md.get("url", "") or ""
                title = md.get("title", "") or ""
                doc_id = md.get("doc_id", "") or ""
                vec = embeddings[i]
                dim = len(vec)
                unique_hash = _sha1(f"{url}||{title}||{text}")
                if RagChunk.objects.filter(unique_hash=unique_hash).exists():
                    continue
                to_create.append(
                    RagChunk(
                        unique_hash=unique_hash,
                        doc_id=doc_id,
                        url=url,
                        title=title,
                        text=text,
                        meta=md,
                        embedding=_to_bytes(vec),
                        dim=dim,
                    )
                )
            if to_create:
                RagChunk.objects.bulk_create(to_create, ignore_conflicts=True)

    def query(
        self,
        query_texts: List[str],
        n_results: int = 5,
        where: Optional[Dict[str, Any]] = None,
        embeddings: Optional[List[List[float]]] = None,
    ) -> Dict[str, List[List[Any]]]:
        if not query_texts:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}

        if embeddings is None:
            embeddings = embed_texts(query_texts)

        qs: QuerySet[RagChunk] = RagChunk.objects.all()
        if where:
            if "doc_id" in where:
                qs = qs.filter(doc_id=where["doc_id"])
            if "url" in where:
                qs = qs.filter(url=where["url"])

        total = qs.count()
        BATCH = 2000

        out_ids: List[List[str]] = []
        out_docs: List[List[str]] = []
        out_metas: List[List[Dict[str, Any]]] = []
        out_dists: List[List[float]] = []

        for qvec in embeddings:
            q = np.asarray(qvec, dtype=np.float32)
            heap: List[Tuple[float, RagChunk]] = []  # (neg_sim, obj)

            # 배치로 전체 스캔 (데이터 커지면 sqlite-vec로 전환 권장)
            for offset in range(0, total, BATCH):
                for c in qs.only("id", "text", "title", "url", "meta", "embedding", "dim")[offset:offset+BATCH]:
                    if c.dim != len(q):
                        continue
                    v = _from_bytes(c.embedding)
                    sim = _cosine_sim(q, v)
                    heap.append((-sim, c))

            heap.sort()
            top = heap[:n_results]

            ids = [str(c.id) for _, c in top]
            docs = [c.text for _, c in top]
            metas = [c.meta for _, c in top]
            dists = [1.0 - (-neg_sim) for (neg_sim, _) in top]  # 1 - cos

            out_ids.append(ids)
            out_docs.append(docs)
            out_metas.append(metas)
            out_dists.append(dists)

        return {
            "ids": out_ids,
            "documents": out_docs,
            "metadatas": out_metas,
            "distances": out_dists,
        }

# =========[ 기존 코드와의 얇은 호환 API ]====================================
_singleton: Optional[SQLiteCollection] = None

def chroma_collection() -> SQLiteCollection:
    global _singleton
    if _singleton is None:
        _singleton = SQLiteCollection()
    return _singleton

def chroma_count() -> int:
    return RagChunk.objects.count()
