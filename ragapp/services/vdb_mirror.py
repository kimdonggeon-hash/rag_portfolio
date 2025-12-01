from __future__ import annotations

import hashlib
from array import array
from typing import Sequence, Mapping, Any, Dict

from django.conf import settings

def _sha256_hex(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

def _vec_to_f32_bytes(vec: Any) -> bytes:
    # numpy 없이 float32 bytes 만들기
    a = array("f", [float(x) for x in vec])
    return a.tobytes()

def django_upsert_ragchunks(
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

        m = dict(meta) if isinstance(meta, Mapping) else {}
        m.setdefault("vdb_id", sid)

        # ✅ URL 원본은 meta에 보관, 필드에는 200 제한 적용
        url_full = str(m.get("url") or "").strip()
        if url_full:
            m.setdefault("url_full", url_full)

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
