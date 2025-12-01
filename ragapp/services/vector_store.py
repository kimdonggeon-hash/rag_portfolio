# ragapp/services/vector_store.py
from __future__ import annotations

import os
import hashlib
import logging
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings

log = logging.getLogger(__name__)


def _sha(txt: str) -> str:
    return hashlib.sha256((txt or "").encode("utf-8", "ignore")).hexdigest()


# ─────────────────────────────────────────────────────────
# 경로 유효성 검사/정규화
#  - Windows에서 금지된 제어문자(< 0x20)나 특수문자(< > : " | ? *)가 섞인 경우
#    그대로 DB를 열려고 하면 WinError 123 이 터질 수 있어, 그런 경우는 백엔드 비활성화
# ─────────────────────────────────────────────────────────
def _normalize_path(raw: Any) -> Optional[str]:
    """
    settings / 환경변수에서 가져온 경로 문자열을 간단히 검증/정규화한다.
    - 제어문자(ASCII < 32)가 포함되면 None 반환
    - Windows 금지 문자(< > : \" | ? *) 포함 시도 None 반환
    """
    if not raw:
        return None

    try:
        s = os.fspath(raw)
    except TypeError:
        s = str(raw)

    # 제어문자(특히 \x0b 등) 들어있으면 그대로 사용하면 WinError 123 발생 가능
    for ch in s:
        if ord(ch) < 32:
            log.error(
                "벡터 DB 경로에 제어문자(0x%02X)가 포함되어 있어 사용하지 않습니다: %r",
                ord(ch),
                s,
            )
            return None

    # Windows 경로에서 사용할 수 없는 문자들
    if any(c in s for c in '<>:"|?*'):
        log.error(
            "벡터 DB 경로에 Windows에서 사용할 수 없는 문자가 포함되어 있어 사용하지 않습니다: %r",
            s,
        )
        return None

    # 기본 정규화
    return os.path.normpath(s)


# ─────────────────────────────────────────────────────────
# 백엔드 준비
#  - sqlite: 네 프로젝트의 "chroma_store.chroma_collection()" (SQLite 기반 래퍼)
#  - chroma: chromadb.PersistentClient(path=CHROMA_DB_DIR)
# 둘 다 설정되어 있으면 동시 활성
# ─────────────────────────────────────────────────────────
def _get_sqlite_collection():
    """
    SQLite 기반 벡터 스토어 래퍼.
    - ragapp.services.chroma_store.chroma_collection() 이 내부에서
      VECTOR_DB_PATH 등을 알아서 처리하도록 맡긴다.
    - 에러가 나면 해당 백엔드는 비활성화.
    """
    try:
        from ragapp.services.chroma_store import chroma_collection as _sqlite_collection

        return _sqlite_collection()
    except Exception as e:  # pragma: no cover
        log.debug("sqlite collection 불러오기 실패(무시): %s", e)
        return None


def _get_chroma_collection():
    """
    DuckDB + ChromaDB 백엔드.
    - CHROMA_COLLECTION / CHROMA_DB_DIR 이 모두 설정된 경우에만 시도.
    - CHROMA_DB_DIR 에 제어문자나 Windows 금지 문자가 있으면 사용하지 않음.
    """
    try:
        import chromadb  # type: ignore
    except Exception as e:  # pragma: no cover
        log.debug("chromadb 미설치/불가(무시): %s", e)
        return None

    coll_name = getattr(settings, "CHROMA_COLLECTION", None) or os.environ.get(
        "CHROMA_COLLECTION"
    )
    db_dir_raw = getattr(settings, "CHROMA_DB_DIR", None) or os.environ.get(
        "CHROMA_DB_DIR"
    )

    if not coll_name or not db_dir_raw:
        return None

    # ✅ 여기서 경로 유효성 체크 / 정규화 (WinError 123 방지용)
    db_dir = _normalize_path(db_dir_raw)
    if not db_dir:
        # 이미 _normalize_path 내부에서 로그를 남겼으니 여기서는 조용히 빠진다.
        return None

    try:
        client = chromadb.PersistentClient(path=db_dir)
        coll = client.get_or_create_collection(
            name=str(coll_name), metadata={"hnsw:space": "cosine"}
        )
        return coll
    except Exception as e:  # pragma: no cover
        log.warning("Chroma 연결 실패(무시): %s", e)
        return None


def _enabled_backends() -> List[Tuple[str, Any]]:
    """
    사용 가능한 벡터 백엔드 목록을 반환.
    - ("sqlite", <collection>)
    - ("chroma", <collection>)
    순서대로 append.
    """
    backends: List[Tuple[str, Any]] = []

    # SQLite 벡터 DB: chroma_store 래퍼가 정상 동작하면 활성
    s = _get_sqlite_collection()
    if s is not None:
        backends.append(("sqlite", s))

    # DuckDB Chroma: 환경/설정이 세팅된 경우만
    c = _get_chroma_collection()
    if c is not None:
        backends.append(("chroma", c))

    return backends


# ─────────────────────────────────────────────────────────
# 공통 Upsert
# docs/metas/embeddings는 동일 길이. ids 없으면 자동 생성
# (메타의 doc_id 우선 → 없으면 문서 SHA 요약)
# ─────────────────────────────────────────────────────────
def multi_upsert_texts(
    *,
    documents: List[str],
    metadatas: List[Dict[str, Any]],
    embeddings: Optional[List[List[float]]] = None,
    ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    n = len(documents or [])
    if n == 0:
        return {"ok": [], "errors": [], "count": 0}

    if metadatas is None or len(metadatas) != n:
        raise ValueError("metadatas 길이가 documents와 다릅니다.")

    if embeddings is not None and len(embeddings) != n:
        raise ValueError("embeddings 길이가 documents와 다릅니다.")

    # ids 준비
    if not ids:
        ids = []
        for i in range(n):
            base = metadatas[i].get("doc_id") or _sha(documents[i])[:24]
            ids.append(str(base))

    results: Dict[str, Any] = {"ok": [], "errors": [], "count": n}

    for name, coll in _enabled_backends():
        try:
            # Chroma 호환 시그니처
            coll.add(
                documents=documents,
                metadatas=metadatas,
                embeddings=embeddings,
                ids=ids,
            )
            results["ok"].append(name)
        except TypeError as e:
            # 일부 구현은 embeddings 생략 불가 → 재시도 안내
            results["errors"].append({"backend": name, "error": f"{e}"})
        except Exception as e:
            results["errors"].append({"backend": name, "error": f"{e}"})

    return results


# ─────────────────────────────────────────────────────────
# 공통 Query (임베딩으로 검색) → 두 백엔드 결과 합쳐 상위 k
# 반환 형태: {hits: [{doc, meta, distance, backend}], debug:{...}}
# ─────────────────────────────────────────────────────────
def multi_query_by_embedding(
    *,
    query_embedding: List[float],
    k: int = 8,
) -> Dict[str, Any]:
    all_hits: List[Dict[str, Any]] = []
    dbg: Dict[str, Any] = {}

    for name, coll in _enabled_backends():
        try:
            res = coll.query(query_embeddings=[query_embedding], n_results=k)

            docs = (res.get("documents") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            dists = (res.get("distances") or [[]])[0]

            # 일부 구현은 distances 대신 similarities 제공 가능성
            if (not dists) and res.get("similarities"):
                # similarities: 높을수록 유사 → distance = 1 - sim 가정
                sims = (res.get("similarities") or [[]])[0]
                dists = [1.0 - float(s) for s in sims]

            for d, m, dist in zip(docs, metas, dists):
                all_hits.append(
                    {
                        "doc": d,
                        "meta": m,
                        "distance": float(dist) if dist is not None else 1.0,
                        "backend": name,
                    }
                )
            dbg[name] = {"count": len(docs)}
        except Exception as e:
            dbg[name] = {"error": str(e)}

    # distance 오름차순 (작을수록 유사)
    all_hits.sort(key=lambda x: x.get("distance", 1e9))
    return {"hits": all_hits[:k], "debug": dbg}
