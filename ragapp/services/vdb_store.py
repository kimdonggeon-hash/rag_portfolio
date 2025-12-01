# ragapp/services/vdb_store.py
from __future__ import annotations

import os
import json
import sqlite3
import time
import logging
import hashlib
from pathlib import Path
from typing import Sequence, Mapping, Any, List, Dict

from django.conf import settings

log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# SQLite 벡터 스토어 경로
#   - settings.VECTOR_DB_PATH
#   - ENV VECTOR_DB_PATH
#   - 기본: BASE_DIR/sqlite3/vector_store.sqlite3
# ─────────────────────────────────────────
def _vdb_path() -> str:
    p = getattr(settings, "VECTOR_DB_PATH", None) or os.environ.get("VECTOR_DB_PATH")
    if not p:
        base = getattr(settings, "BASE_DIR", Path.cwd())
        p = str(Path(base) / "sqlite3" / "vector_store.sqlite3")
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect() -> sqlite3.Connection:
    path = _vdb_path()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS embeddings(
            id TEXT PRIMARY KEY,
            doc TEXT,
            meta TEXT,          -- JSON (UTF-8)
            embedding TEXT,     -- JSON array of floats
            dim INTEGER,
            updated_at TEXT
        );
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_dim ON embeddings(dim);")


def _sha256_hex(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8", errors="ignore")).hexdigest()


def _json_safe_obj(obj: Any) -> Any:
    """
    JSONField/JSON 저장 시 직렬화 불가 타입(datetime 등)이 섞여도 터지지 않게 안전 변환.
    """
    try:
        return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
    except Exception:
        return {}


def _vec_to_f32_bytes(vec: Sequence[float]) -> bytes:
    """
    RagChunk.embedding(BinaryField)에 넣기 위한 float32 bytes.
    - numpy 있으면 np.float32로 고정
    - 없으면 array('f')로 fallback
    """
    try:
        import numpy as np  # type: ignore
        return np.asarray(list(vec), dtype=np.float32).tobytes()
    except Exception:
        from array import array
        arr = array("f", (float(x) for x in vec))
        return arr.tobytes()


# ─────────────────────────────────────────
# 내부: SQLite 업서트 실제 구현
# ─────────────────────────────────────────
def _sqlite_upsert(
    ids: Sequence[str],
    docs: Sequence[str],
    metas: Sequence[Mapping[str, Any]],
    embs: Sequence[Sequence[float]],
) -> Dict[str, Any]:
    if not (len(ids) == len(docs) == len(metas) == len(embs)):
        raise ValueError("vdb_upsert: ids/docs/metas/embs 길이가 일치해야 합니다.")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn = _connect()
    try:
        _ensure_schema(conn)
        cur = conn.cursor()
        inserted = 0
        last_dim = None

        for i, doc, meta, vec in zip(ids, docs, metas, embs):
            if not isinstance(i, str) or not i.strip():
                continue

            vec_list = [float(x) for x in vec]
            last_dim = len(vec_list)

            # meta가 직렬화 불가능 타입을 포함해도 안전하게 저장
            meta_safe = _json_safe_obj(dict(meta) if isinstance(meta, Mapping) else {})

            cur.execute(
                """
                INSERT INTO embeddings(id, doc, meta, embedding, dim, updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  doc=excluded.doc,
                  meta=excluded.meta,
                  embedding=excluded.embedding,
                  dim=excluded.dim,
                  updated_at=excluded.updated_at
                """,
                (
                    i.strip(),
                    doc or "",
                    json.dumps(meta_safe, ensure_ascii=False),
                    json.dumps(vec_list),
                    last_dim,
                    now,
                ),
            )
            inserted += 1

        conn.commit()
        return {
            "ok": True,
            "inserted": inserted,
            "path": _vdb_path(),
            "dim": last_dim,
        }
    finally:
        conn.close()


# ─────────────────────────────────────────
# ✅ Django 모델(RagChunk)로 미러링 업서트
#   - admin에서 /admin/ragapp/ragchunk/ 로 확인 가능
#   - 실패해도 SQLite는 이미 성공했으니 기능은 계속 돌아가게 설계
# ─────────────────────────────────────────
def _django_upsert_ragchunks(
    ids: Sequence[str],
    docs: Sequence[str],
    metas: Sequence[Mapping[str, Any]],
    embs: Sequence[Sequence[float]],
) -> Dict[str, Any]:
    enabled = bool(getattr(settings, "RAGCHUNK_MIRROR_ENABLED", True))
    if not enabled:
        return {"status": "disabled"}

    try:
        from ragapp.models import RagChunk  # 늦은 import (AppRegistry 문제 방지)
    except Exception as e:
        return {"status": f"error_import: {e}"}

    rows = []
    skipped = 0

    for i, doc, meta, vec in zip(ids, docs, metas, embs):
        sid = (str(i) if i is not None else "").strip()
        if not sid:
            skipped += 1
            continue

        m0 = dict(meta) if isinstance(meta, Mapping) else {}
        m0.setdefault("vdb_id", sid)

        # url 원문은 meta에, 필드에는 200자 제한 맞춤
        url_full = str(m0.get("url") or "")
        m0.setdefault("url_full", url_full)

        # JSONField 안전 변환
        m = _json_safe_obj(m0)

        doc_id = str(m.get("doc_id") or sid)[:191]
        url = url_full[:200]
        title = str(m.get("title") or "")[:500]
        text = (doc or "").strip()
        if not text:
            skipped += 1
            continue

        try:
            emb_bytes = _vec_to_f32_bytes(vec)
            dim = int(len(vec))
        except Exception:
            skipped += 1
            continue

        rows.append(
            RagChunk(
                unique_hash=_sha256_hex(sid),
                doc_id=doc_id,
                url=url,
                title=title,
                text=text,
                meta=m,
                embedding=emb_bytes,
                dim=dim,
            )
        )

    if not rows:
        return {"status": "empty", "skipped": skipped}

    try:
        RagChunk.objects.bulk_create(
            rows,
            batch_size=int(getattr(settings, "RAGCHUNK_MIRROR_BATCH", 500)),
            update_conflicts=True,
            unique_fields=["unique_hash"],
            update_fields=["doc_id", "url", "title", "text", "meta", "embedding", "dim"],
        )
        return {"status": "ok", "upserted": len(rows), "skipped": skipped}
    except Exception as e:
        return {"status": f"error_upsert: {e}", "attempted": len(rows), "skipped": skipped}


# ✅ 외부 호출/가독성용 별칭(이게 없으면 '정의 안 됨' 에러 나기 쉬움)
django_upsert_ragchunks = _django_upsert_ragchunks


# ─────────────────────────────────────────
# Chroma 초기화 (옵션)
#   - settings.CHROMA_DB_DIR / ENV CHROMA_DB_DIR
#   - 기본: BASE_DIR/chroma
# ─────────────────────────────────────────
_chroma_client = None
_chroma_collection = None
_chroma_error = None


def _get_chroma_collection():
    global _chroma_client, _chroma_collection, _chroma_error

    if _chroma_collection is not None:
        return _chroma_collection
    if _chroma_error is not None:
        raise RuntimeError(_chroma_error)

    try:
        import chromadb
        from chromadb.config import Settings as ChromaSettings  # type: ignore
    except Exception as e:
        _chroma_error = f"chromadb 임포트 실패: {e}"
        raise RuntimeError(_chroma_error)

    db_dir = getattr(settings, "CHROMA_DB_DIR", None) or os.environ.get("CHROMA_DB_DIR")
    if not db_dir:
        base = getattr(settings, "BASE_DIR", Path.cwd())
        db_dir = str(Path(base) / "chroma")

    Path(db_dir).mkdir(parents=True, exist_ok=True)

    collection_name = getattr(settings, "CHROMA_COLLECTION", "rag-default")

    try:
        _chroma_client = chromadb.PersistentClient(
            path=db_dir,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        _chroma_collection = _chroma_client.get_or_create_collection(name=collection_name)
        log.info("Chroma collection ready: dir=%s, collection=%s", db_dir, collection_name)
        return _chroma_collection
    except Exception as e:
        _chroma_error = f"Chroma 초기화 실패: {e}"
        raise RuntimeError(_chroma_error)


# ─────────────────────────────────────────
# 외부에 노출되는 공통 업서트 진입점
#   - 항상 SQLite에 쓰고
#   - 가능하면 Chroma에도 같이 업서트
#   - ✅ 옵션: Django RagChunk로 미러링 업서트
# ─────────────────────────────────────────
def vdb_upsert(
    ids: Sequence[str],
    docs: Sequence[str],
    metas: Sequence[Mapping[str, Any]],
    embs: Sequence[Sequence[float]],
) -> Dict[str, Any]:
    # 1) 메인: SQLite
    sqlite_result = _sqlite_upsert(ids, docs, metas, embs)

    # 1.5) Django RagChunk 미러링 (옵션)
    dj = {"status": "disabled"}
    if getattr(settings, "VDB_MIRROR_TO_RAGCHUNK", False):
        try:
            dj = _django_upsert_ragchunks(ids, docs, metas, embs)  # ✅ 언더바 붙은 함수로 호출
        except Exception as e:
            dj = {"status": f"error_exception: {e}"}
    sqlite_result["django_ragchunk"] = dj  # ✅ 딱 1번만 세팅

    # 2) 보조: Chroma
    chroma_status: str = "disabled"
    try:
        col = _get_chroma_collection()
    except Exception as e:
        log.warning("Chroma 사용 불가 (SQLite만 사용): %s", e)
        chroma_status = f"error_init: {e}"
    else:
        try:
            ids_list: List[str] = [str(i) for i in ids]
            docs_list: List[str] = [str(d or "") for d in docs]
            embs_list: List[List[float]] = [[float(x) for x in v] for v in embs]
            metas_list = [_json_safe_obj(dict(m)) for m in metas]
            col.upsert(ids=ids_list, documents=docs_list, metadatas=metas_list, embeddings=embs_list)
            chroma_status = "ok"
        except Exception as e:
            log.warning("Chroma 업서트 실패 (SQLite는 이미 성공): %s", e)
            chroma_status = f"error_upsert: {e}"

    sqlite_result["chroma"] = chroma_status
    return sqlite_result



# ─────────────────────────────────────────
# 선택: 카운트/초기화/정보 (SQLite 기준)
# ─────────────────────────────────────────
def vdb_count() -> int:
    conn = _connect()
    try:
        _ensure_schema(conn)
        c = conn.execute("SELECT COUNT(*) FROM embeddings")
        return int(c.fetchone()[0] or 0)
    finally:
        conn.close()


def vdb_clear() -> None:
    conn = _connect()
    try:
        _ensure_schema(conn)
        conn.execute("DELETE FROM embeddings")
        conn.commit()
    finally:
        conn.close()


def vdb_info() -> Dict[str, Any]:
    return {
        "path": _vdb_path(),
        "count": vdb_count(),
    }
