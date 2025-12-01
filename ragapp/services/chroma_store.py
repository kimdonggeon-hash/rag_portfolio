# ragapp/services/chroma_store.py
from pathlib import Path
import importlib
from typing import List, Dict, Any, Optional
from django.conf import settings

# ✅ 변경: 임베딩 유틸 import
#   - embed_texts 는 필수
#   - current_embed_dim 은 있으면 사용, 없으면 로컬 _want_embed_dim 기본값 사용
from .vertex_embed import embed_texts  # (필수)

try:
    # vertex_embed에 current_embed_dim()를 만들어두었다면 사용 (선택)
    from .vertex_embed import current_embed_dim as _current_embed_dim  # (선택)
except Exception:
    _current_embed_dim = None

from .utils import normalize_where_filter


def _chroma_client():
    chromadb = importlib.import_module("chromadb")
    # settings.CHROMA_DB_DIR 은 settings.py에서 _canon()을 통해
    # 제어문자(\x0b 등) 제거 + 정규화된 값이 들어오도록 이미 처리되어 있음.
    Path(settings.CHROMA_DB_DIR).mkdir(parents=True, exist_ok=True)
    PersistentClient = getattr(chromadb, "PersistentClient", None)
    if PersistentClient:
        return PersistentClient(path=settings.CHROMA_DB_DIR)
    # 구버전 fallback
    from chromadb.config import Settings as _S
    return chromadb.Client(
        _S(chroma_db_impl="duckdb+parquet", persist_directory=settings.CHROMA_DB_DIR)
    )


# ✅ 임베딩 차원 결정 헬퍼 (vertex_embed에 없으면 이 로컬 함수가 사용됨)
def _want_embed_dim() -> int:
    """
    사용 중인 임베딩 모델의 벡터 차원.
    - vertex_embed.current_embed_dim() 있으면 그것 사용
    - 없으면 모델명 기준으로 합리적 기본값(대표적으로 text-embedding-004 => 768)
    """
    if callable(_current_embed_dim):
        try:
            return int(_current_embed_dim())
        except Exception:
            pass

    model = getattr(settings, "VERTEX_EMBED_MODEL", "text-embedding-004")
    dim_map = {
        "text-embedding-004": 768,
        "text-multilingual-embedding-002": 768,  # 필요시 다른 모델 추가
    }
    return dim_map.get(model, 768)


def chroma_collection():
    """
    현재 임베딩 차원에 맞는 컬렉션을 가져오거나 자동 생성.
    기존 컬렉션 차원이 다르면 "컬렉션명_dim" 으로 새로 만든다.
    """
    c = _chroma_client()
    base = settings.CHROMA_COLLECTION
    want_dim = _want_embed_dim()

    cur_dim = -1
    try:
        col = c.get_or_create_collection(name=base)
        try:
            got = col.get(limit=1, include=["embeddings"])
            embs = got.get("embeddings") or []
            if embs and embs[0]:
                cur_dim = len(embs[0])
        except Exception:
            pass
        if cur_dim in (-1, None) or cur_dim == want_dim:
            return col
    except Exception:
        pass

    alt = f"{base}_{want_dim}"
    # 로그는 여기서 print 대신 조용히 fallback
    return c.get_or_create_collection(name=alt)


def chroma_upsert(
    ids: List[str],
    docs: List[str],
    metas: List[Dict[str, Any]],
    embs: List[List[float]],
):
    """
    upsert 지원 안하는 구버전 대응까지 포함
    """
    col = chroma_collection()
    if hasattr(col, "upsert"):
        return col.upsert(ids=ids, documents=docs, metadatas=metas, embeddings=embs)

    # 구버전은 add 전 중복 제거 필요
    try:
        col.delete(ids=ids)
    except Exception:
        pass
    return col.add(ids=ids, documents=docs, metadatas=metas, embeddings=embs)


def chroma_count(col=None) -> int:
    try:
        col = col or chroma_collection()
        if hasattr(col, "count"):
            return int(col.count())
        data = col.get(limit=1_000_000)
        return len(data.get("ids") or [])
    except Exception:
        return 0


def chroma_query_with_embeddings(
    col,
    query: str,
    topk: int,
    where=None,
    include: Optional[List[str]] = None,
):
    """
    질문을 임베딩해서 Chroma에 질의.
    where 필터, include 필드(문서/메타/거리)도 지원.
    """
    # ✅ Vertex 임베딩으로 질의 벡터 생성
    q_emb = embed_texts([query])[0]
    inc = include or ["documents", "metadatas", "distances"]

    where = normalize_where_filter(where)

    try:
        # 최신 chroma: where 지원
        return col.query(
            query_embeddings=[q_emb],
            n_results=max(1, int(topk)),
            where=where,
            include=inc,
        )
    except TypeError:
        # where 미지원
        try:
            return col.query(
                query_embeddings=[q_emb],
                n_results=max(1, int(topk)),
                include=inc,
            )
        except TypeError:
            # 아주 구버전
            return col.query(query_embeddings=[q_emb], n_results=max(1, int(topk)))
