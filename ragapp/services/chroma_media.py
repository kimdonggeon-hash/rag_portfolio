from __future__ import annotations

import os
import json
import mimetypes
import hashlib
from pathlib import Path
from typing import List, Dict, Any

import chromadb
from chromadb.config import Settings

CHROMA_MEDIA_DIR = os.getenv("CHROMA_MEDIA_DIR", "chroma_media")


def _client() -> chromadb.Client:
    Path(CHROMA_MEDIA_DIR).mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(settings=Settings(persist_directory=CHROMA_MEDIA_DIR))


def images_coll():
    return _client().get_or_create_collection(name="media_images")


def table_coll():
    return _client().get_or_create_collection(name="table_rows")


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _guess_mime(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def add_image_item(*, path: str, embedding: List[float], caption: str = "") -> str:
    c = images_coll()
    p = Path(path).resolve()
    fid = _sha256_file(str(p))
    stat = p.stat()
    pid = f"img:{fid}:{stat.st_size}:{int(stat.st_mtime)}"
    meta = {
        "path": str(p),
        "mime": _guess_mime(str(p)),
        "size": int(stat.st_size),
        "mtime": int(stat.st_mtime),
    }
    try:
        c.add(
            ids=[pid],
            embeddings=[embedding],
            documents=[caption or p.name],
            metadatas=[meta],
        )
    except Exception:
        try:
            c.delete(ids=[pid])
        except Exception:
            pass
        c.add(
            ids=[pid],
            embeddings=[embedding],
            documents=[caption or p.name],
            metadatas=[meta],
        )
    return pid


def search_images_by_text_embedding(*, text_embedding: List[float], k: int = 8):
    c = images_coll()
    return c.query(query_embeddings=[text_embedding], n_results=int(k))


def add_table_rows(
    *, table_name: str, rows: List[Dict[str, Any]], embeddings: List[List[float]]
) -> int:
    """표 한 줄당 1개 벡터로 table_rows 컬렉션에 넣기."""
    if len(rows) != len(embeddings):
        raise ValueError("rows와 embeddings 길이가 다릅니다.")

    c = table_coll()

    ids: List[str] = []
    docs: List[str] = []
    metas: List[Dict[str, Any]] = []

    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            row = {"value": row}

        pid = f"row:{table_name}:{i:08d}"
        doc = " | ".join(f"{k}:{row.get(k, '')}" for k in row.keys())

        try:
            row_json = json.dumps(row, ensure_ascii=False)
        except Exception:
            row_json = json.dumps({k: str(v) for k, v in row.items()}, ensure_ascii=False)

        # ⚠️ Chroma 메타데이터는 리스트를 허용하지 않으니, 딱 필요한 것만 단순 타입으로 저장
        meta = {
            "table": table_name,
            "row_json": row_json,
        }

        ids.append(pid)
        docs.append(doc[:2000])
        metas.append(meta)

    B = 512
    for b in range(0, len(ids), B):
        c.add(
            ids=ids[b : b + B],
            embeddings=embeddings[b : b + B],
            documents=docs[b : b + B],
            metadatas=metas[b : b + B],
        )

    return len(ids)


def search_table_by_text_embedding(*, text_embedding: List[float], k: int = 10):
    c = table_coll()
    return c.query(query_embeddings=[text_embedding], n_results=int(k))
