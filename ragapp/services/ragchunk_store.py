# ragapp/services/ragchunk_store.py
from __future__ import annotations

from typing import Any, Iterable

from django.db import transaction
from ragapp.models import RagChunk


def _to_float32_bytes(vec: Any) -> tuple[bytes, int]:
    """
    vec: list[float] | np.ndarray 등
    return: (bytes(float32), dim)
    """
    if vec is None:
        return b"", 0

    # numpy 있으면 그걸 우선
    try:
        import numpy as np  # type: ignore
        arr = np.asarray(vec, dtype=np.float32)
        return arr.tobytes(), int(arr.size)
    except Exception:
        pass

    # 표준라이브러리 fallback
    from array import array
    a = array("f", [float(x) for x in vec])
    return a.tobytes(), len(a)


@transaction.atomic
def upsert_ragchunks(
    *,
    unique_hashes: list[str],
    texts: list[str],
    metas: list[dict],
    embeddings: list[Any],
) -> dict:
    """
    unique_hash 기준으로 upsert 해서 RagChunk 채움.
    - 기존 있으면 update
    - 없으면 create
    """
    n = min(len(unique_hashes), len(texts), len(metas), len(embeddings))
    if n <= 0:
        return {"saved": 0, "created": 0, "updated": 0}

    uh = unique_hashes[:n]
    tx = texts[:n]
    mt = metas[:n]
    em = embeddings[:n]

    existing = {
        o.unique_hash: o
        for o in RagChunk.objects.filter(unique_hash__in=uh).only("id", "unique_hash")
    }

    to_create: list[RagChunk] = []
    to_update: list[RagChunk] = []

    for i in range(n):
        h = str(uh[i])[:64]
        text = (tx[i] or "")
        meta = mt[i] or {}
        emb_bytes, dim = _to_float32_bytes(em[i])

        doc_id = str(meta.get("doc_id") or meta.get("document_id") or meta.get("url") or "")[:191]
        url = str(meta.get("url") or "")
        title = str(meta.get("title") or "")[:500]

        if h in existing:
            obj = RagChunk(
                id=existing[h].id,  # update target
                unique_hash=h,
                doc_id=doc_id,
                url=url,
                title=title,
                text=text,
                meta=meta,
                embedding=emb_bytes,
                dim=dim,
            )
            to_update.append(obj)
        else:
            to_create.append(
                RagChunk(
                    unique_hash=h,
                    doc_id=doc_id,
                    url=url,
                    title=title,
                    text=text,
                    meta=meta,
                    embedding=emb_bytes,
                    dim=dim,
                )
            )

    created = 0
    updated = 0

    if to_create:
        RagChunk.objects.bulk_create(to_create, batch_size=500)
        created = len(to_create)

    if to_update:
        RagChunk.objects.bulk_update(
            to_update,
            fields=["doc_id", "url", "title", "text", "meta", "embedding", "dim"],
            batch_size=500,
        )
        updated = len(to_update)

    return {"saved": created + updated, "created": created, "updated": updated}
