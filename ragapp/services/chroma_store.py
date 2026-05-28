# ragapp/services/chroma_store.py
from __future__ import annotations

from pathlib import Path
import importlib
import os
import re
from typing import List, Dict, Any, Optional

from django.conf import settings

# ✅ 임베딩 유틸
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

    # settings.CHROMA_DB_DIR 은 settings.py에서 경로 정규화/제어문자 제거가 끝난 값이라고 가정
    Path(settings.CHROMA_DB_DIR).mkdir(parents=True, exist_ok=True)

    PersistentClient = getattr(chromadb, "PersistentClient", None)
    if PersistentClient:
        return PersistentClient(path=settings.CHROMA_DB_DIR)

    # 구버전 fallback
    from chromadb.config import Settings as _S  # type: ignore
    return chromadb.Client(
        _S(chroma_db_impl="duckdb+parquet", persist_directory=settings.CHROMA_DB_DIR)
    )


def _embed_model_name() -> str:
    """
    현재 '실제로 사용하는' 임베딩 모델명을 결정.
    - llm_vertex.py의 env 강제 정책과 동일한 우선순위로 맞춤
      1) VERTEX_EMBED_MODEL
      2) GEMINI_EMBED_MODELS (comma 가능, 첫 항목)
      3) GEMINI_EMBED_MODEL
      4) settings.VERTEX_EMBED_MODEL (있으면)
      5) default fallback
    """
    vtx = (os.environ.get("VERTEX_EMBED_MODEL") or "").strip()
    if vtx:
        return vtx

    multi = (os.environ.get("GEMINI_EMBED_MODELS") or "").strip()
    if multi:
        first = multi.split(",")[0].strip()
        if first:
            return first

    single = (os.environ.get("GEMINI_EMBED_MODEL") or "").strip()
    if single:
        return single

    st = (getattr(settings, "VERTEX_EMBED_MODEL", "") or "").strip()
    if st:
        return st

    # 기본은 005로(네가 교체하려는 방향)
    return "text-embedding-005"


def _safe_col_token(s: str) -> str:
    """
    Chroma 컬렉션명 토큰 안전화:
    - 허용 문자 외는 '-' 치환
    - 너무 길면 자름(운영 사고 방지)
    """
    s = (s or "").strip()
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", s)
    s = s.strip("-") or "embed"
    return s[:60]


def _want_embed_dim() -> int:
    """
    사용 중인 임베딩 모델의 벡터 차원.
    - vertex_embed.current_embed_dim() 있으면 그 값을 우선 사용
    - 없으면 모델명 기준으로 합리적 기본값을 사용
    """
    if callable(_current_embed_dim):
        try:
            d = int(_current_embed_dim())
            if d > 0:
                return d
        except Exception:
            pass

    model = _embed_model_name()
    dim_map = {
        "text-embedding-004": 768,
        "text-embedding-005": 768,
        "text-multilingual-embedding-002": 768,
        # 필요시 추가:
        # "gemini-embedding-001": 3072,  # output_dimensionality로 줄여 쓸 수도 있음
    }
    return int(dim_map.get(model, 768))


def _collection_name(base: str) -> str:
    """
    ✅ 핵심: '모델+차원' 기준으로 컬렉션을 분리해서
    text-embedding-004(768)과 text-embedding-005(768)이 같은 컬렉션에 섞이는 걸 방지.
    """
    want_model = _embed_model_name()
    want_dim = _want_embed_dim()
    return f"{base}__{_safe_col_token(want_model)}_{want_dim}"


def chroma_collection():
    """
    현재 임베딩 모델/차원에 맞는 컬렉션을 가져오거나 자동 생성.
    - 항상 base__<model>_<dim> 형태로 생성/사용 (혼입 방지)
    """
    c = _chroma_client()
    base = getattr(settings, "CHROMA_COLLECTION", None) or "chroma_default"
    name = _collection_name(str(base))
    return c.get_or_create_collection(name=name)


def chroma_upsert(
    ids: List[str],
    docs: List[str],
    metas: List[Dict[str, Any]],
    embs: List[List[float]],
):
    """
    upsert 지원 안하는 구버전 대응까지 포함.
    + 메타에 embedding_model/embedding_dim를 기본 기록(디버깅/검증용)
    """
    col = chroma_collection()

    want_model = _embed_model_name()
    want_dim = _want_embed_dim()

    metas2: List[Dict[str, Any]] = []
    for m in metas or []:
        mm = dict(m or {})
        mm.setdefault("embedding_model", want_model)
        mm.setdefault("embedding_dim", want_dim)
        metas2.append(mm)

    if hasattr(col, "upsert"):
        return col.upsert(ids=ids, documents=docs, metadatas=metas2, embeddings=embs)

    # 구버전은 add 전 중복 제거 필요
    try:
        col.delete(ids=ids)
    except Exception:
        pass
    return col.add(ids=ids, documents=docs, metadatas=metas2, embeddings=embs)


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
