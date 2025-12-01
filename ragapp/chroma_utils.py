# ragapp/chroma_utils.py
from __future__ import annotations

import os
import importlib
import uuid
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

from django.conf import settings

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
#  임베딩: 1순위 Vertex(gemini_client), 2순위 로컬 SBERT
# ─────────────────────────────────────────────────────────────

# 중앙 Vertex 임베딩 클라이언트 (없으면 None로 처리)
try:
    from ragapp.services.gemini_client import embed_texts as _vertex_embed_texts
    from ragapp.services.gemini_client import current_embed_dim as _vertex_current_dim
except Exception:  # pragma: no cover
    _vertex_embed_texts = None
    _vertex_current_dim = None

# 로컬 SBERT(한국어 멀티링구얼) 모델 이름
_SBERT_MODEL_NAME = os.environ.get(
    "SBERT_MODEL",
    "jhgan/ko-sroberta-multitask",  # 기본값: 가볍고 한국어 잘 되는 모델
)
_SBERT_MODEL = None


def _ensure_sbert():
    """
    로컬 sentence-transformers 모델을 lazy load.
    Vertex 임베딩이 실패하거나 설정이 없는 경우 폴백용.
    """
    global _SBERT_MODEL
    if _SBERT_MODEL is None:
        from sentence_transformers import SentenceTransformer

        log.info("SBERT 로컬 임베딩 모델 로드: %s", _SBERT_MODEL_NAME)
        _SBERT_MODEL = SentenceTransformer(_SBERT_MODEL_NAME)
    return _SBERT_MODEL


def embed_texts(texts: List[str]) -> List[List[float]]:
    """
    텍스트 배열 → 임베딩 벡터 배열.

    우선순위:
      1) 중앙 Vertex 임베딩(ragapp.services.gemini_client.embed_texts)
      2) 실패 시 로컬 SBERT(sentence_transformers) 폴백

    - Vertex 쪽 설정이 완전히 안 되어 있어도, SBERT만 있으면 서비스는 동작함.
    """
    if not texts:
        return []

    # 1) Vertex 기반 중앙 임베딩 시도
    if _vertex_embed_texts is not None:
        try:
            vecs = _vertex_embed_texts(texts)
            # 간단 sanity 체크: 길이/차원 확인
            if vecs and len(vecs) == len(texts) and isinstance(vecs[0], (list, tuple)):
                return [list(map(float, v)) for v in vecs]
            log.warning("Vertex 임베딩 결과가 비어있거나 형식이 예상과 다릅니다. SBERT로 폴백합니다.")
        except Exception as e:  # pragma: no cover
            log.warning("Vertex 임베딩 실패 → SBERT 폴백: %s", e)

    # 2) 로컬 SBERT 폴백
    m = _ensure_sbert()
    return m.encode(texts, normalize_embeddings=True).tolist()


def _embed_dim() -> int:
    """
    현재 사용 중인 임베딩 차원 추정.

    - 1순위: gemini_client.current_embed_dim (Vertex 기준)
    - 2순위: 이 모듈의 embed_texts("__dim_probe__") 결과 길이
    - 실패 시: 0
    """
    # 1) 중앙 Vertex 헬퍼 우선
    if _vertex_current_dim is not None:
        try:
            d = int(_vertex_current_dim())
            if d > 0:
                return d
        except Exception as e:  # pragma: no cover
            log.debug("vertex current_embed_dim 실패: %s", e)

    # 2) 실제 임베딩 한 번 호출해서 길이 확인
    try:
        v = embed_texts(["__dim_probe__"])[0]
        return len(v)
    except Exception as e:  # pragma: no cover
        log.warning("_embed_dim 추정 실패: %s", e)
        return 0


# ─────────────────────────────────────────────────────────────
#  Chroma 클라이언트 / 컬렉션
# ─────────────────────────────────────────────────────────────

def _chroma_client():
    """
    Chroma 클라이언트 생성.

    - 우선 최신 스타일:
        chromadb.PersistentClient(settings=Settings(persist_directory=...))
    - 실패 시 구버전 스타일:
        chromadb.PersistentClient(path=...) 또는 chromadb.Client(Settings(...))
    """
    chromadb = importlib.import_module("chromadb")
    Path(settings.CHROMA_DB_DIR).mkdir(parents=True, exist_ok=True)

    # 새 버전 Settings
    SettingsCls = None
    try:
        from chromadb.config import Settings as SettingsCls  # type: ignore
    except Exception:
        SettingsCls = None

    PersistentClient = getattr(chromadb, "PersistentClient", None)

    # 1) PersistentClient + Settings(persist_directory=...) 우선
    if PersistentClient and SettingsCls:
        try:
            return PersistentClient(
                settings=SettingsCls(persist_directory=settings.CHROMA_DB_DIR)
            )
        except TypeError:
            # 2) 일부 버전은 path= 인자를 받기도 함
            try:
                return PersistentClient(path=settings.CHROMA_DB_DIR)
            except Exception:
                pass

    # 3) 구버전 Client(Settings(...)) 폴백
    if SettingsCls:
        try:
            return chromadb.Client(
                SettingsCls(
                    chroma_db_impl="duckdb+parquet",
                    persist_directory=settings.CHROMA_DB_DIR,
                )
            )
        except Exception:
            pass

    # 4) 마지막 폴백: 인자 없는 Client (권장 X, 하지만 완전히 막히는 것보단 낫게)
    log.warning("Chroma 최신 설정 사용 실패 → 기본 chromadb.Client()로 폴백합니다.")
    return chromadb.Client()


def get_collection():
    """
    현재 임베딩 차원에 맞는 컬렉션을 반환.

    - 기본 이름: settings.CHROMA_COLLECTION
    - 기존 컬렉션의 임베딩 차원과 안 맞으면: {name}_{dim} 새 컬렉션 사용
    """
    client = _chroma_client()
    base = settings.CHROMA_COLLECTION
    want = _embed_dim()

    # 임베딩 차원을 못 구했으면(0), 그냥 기본 컬렉션으로만 시도
    if want <= 0:
        return client.get_or_create_collection(name=base)

    # 우선 기본 컬렉션 시도
    try:
        col = client.get_or_create_collection(name=base)
        try:
            got = col.get(limit=1, include=["embeddings"])
            embs = (got.get("embeddings") or [])
            if embs and embs[0]:
                cur = len(embs[0])
                if cur == want:
                    return col
        except Exception:
            # 비어 있는 컬렉션이면 그대로 사용
            return col
    except Exception as e:
        log.warning("기본 Chroma 컬렉션(%s) 접근 실패: %s", base, e)

    # 차원이 다르거나 기본 컬렉션 접근 실패 → dim suffix 붙인 새로운 컬렉션 사용
    alt = f"{base}_{want}"
    log.info("임베딩 차원 불일치 또는 오류 → 새 컬렉션 사용: %s", alt)
    return client.get_or_create_collection(name=alt)


# ─────────────────────────────────────────────────────────────
#  편의 함수들 (upsert / count / query / seed)
# ─────────────────────────────────────────────────────────────

def upsert_texts(
    texts: List[str],
    metadatas: Optional[List[Dict[str, Any]]] = None,
    ids: Optional[List[str]] = None,
):
    """
    텍스트 리스트를 현재 컬렉션에 upsert.
    - texts: 문서 본문
    - metadatas: 각 문서에 붙일 메타데이터(dict)
    - ids: 명시적 ID (없으면 UUID 자동 생성)
    """
    texts = [t for t in (texts or []) if (t or "").strip()]
    if not texts:
        return {"inserted": 0}

    embs = embed_texts(texts)
    col = get_collection()

    if ids is None:
        ids = [str(uuid.uuid4()) for _ in texts]
    if metadatas is None:
        metadatas = [{} for _ in texts]

    # Chroma 0.4+는 upsert, 구버전은 add/delete 조합
    if hasattr(col, "upsert"):
        col.upsert(ids=ids, documents=texts, metadatas=metadatas, embeddings=embs)
    else:
        try:
            col.delete(ids=ids)
        except Exception:
            pass
        col.add(ids=ids, documents=texts, metadatas=metadatas, embeddings=embs)

    return {
        "inserted": len(texts),
        "collection": getattr(col, "name", settings.CHROMA_COLLECTION),
    }


def count() -> int:
    """
    현재 컬렉션 내 문서 개수.
    """
    col = get_collection()
    try:
        return int(col.count())
    except Exception:
        data = col.get(limit=1_000_000)
        return len(data.get("ids") or [])


def query(q: str, topk: int = 5):
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
    qv = embed_texts([q])[0]

    res = col.query(
        query_embeddings=[qv],
        n_results=max(1, int(topk)),
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
                "snippet": (d[:500] if isinstance(d, str) else str(d))
                .replace("\n", " ")
                .strip(),
            }
        )
    return hits


def seed_minimal():
    """
    Chroma 동작 확인용 최소 시드 문서 2개를 넣는 유틸.
    """
    texts = [
        "이 문서는 RAG 동작 점검용 샘플입니다. 크로마 DB가 정상인지 확인하세요.",
        "두 번째 문서입니다. 간단한 검색 테스트에 사용됩니다.",
    ]
    metas = [
        {"source": "seed", "title": "doc1"},
        {"source": "seed", "title": "doc2"},
    ]
    return upsert_texts(texts, metas)
