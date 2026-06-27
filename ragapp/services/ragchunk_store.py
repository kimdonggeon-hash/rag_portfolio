# ragapp/services/ragchunk_store.py
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

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

def _safe_url(u: str) -> str:
    u = str(u or "").strip()
    if not u:
        return ""

    try:
        p = urlparse(u)
        if p.scheme in ("http", "https") and p.netloc:
            return u
    except Exception:
        pass

    return ""


def _should_skip_ragchunk(text: str, meta: dict) -> bool:
    """
    검색 후보 품질을 떨어뜨릴 수 있는 청크는 저장하지 않는다.
    """
    text_clean = str(text or "").strip()
    if not text_clean:
        return True

    title = str((meta or {}).get("title") or "")
    source = str((meta or {}).get("source") or (meta or {}).get("source_name") or "")

    text_up = text_clean.upper()
    title_up = title.upper()
    source_up = source.upper()

    # 메타 전용/얇은 청크 제거
    if "[META ONLY]" in text_up:
        return True
    if "[META ONLY]" in title_up:
        return True
    if "[META ONLY]" in source_up:
        return True

    # 너무 짧은 청크는 검색 근거로 쓰기 애매함
    if len(text_clean) < 30:
        return True

    return False

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

    # ✅ unique_hash 정규화
    norm_hashes: list[str] = []
    seen_lookup: set[str] = set()

    for raw_h in uh:
        h = str(raw_h or "").strip()[:64]
        if not h:
            continue
        if h in seen_lookup:
            continue
        seen_lookup.add(h)
        norm_hashes.append(h)

    if not norm_hashes:
        return {"saved": 0, "created": 0, "updated": 0}

    existing = {
        o.unique_hash: o
        for o in RagChunk.objects.filter(unique_hash__in=norm_hashes).only("id", "unique_hash")
    }

    to_create: list[RagChunk] = []
    to_update: list[RagChunk] = []
    seen_in_batch: set[str] = set()

    for i in range(n):
        h = str(uh[i] or "").strip()[:64]
        if not h:
            continue

        # ✅ 같은 호출 안에서 unique_hash 중복 방지
        if h in seen_in_batch:
            continue
        seen_in_batch.add(h)

        text = str(tx[i] or "").strip()
        meta = mt[i] if isinstance(mt[i], dict) else {}

        # ✅ 검색 품질 저하 청크 제외
        if _should_skip_ragchunk(text, meta):
            continue

        try:
            emb_bytes, dim = _to_float32_bytes(em[i])
        except Exception:
            continue

        # ✅ 비정상 임베딩 제외
        if dim <= 0 or not emb_bytes:
            continue

        doc_id = str(meta.get("doc_id") or meta.get("document_id") or meta.get("url") or "")[:191]
        url = _safe_url(meta.get("url") or "")
        title = str(meta.get("title") or "")[:500]

        if h in existing:
            obj = RagChunk(
                id=existing[h].id,
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
        RagChunk.objects.bulk_create(to_create, batch_size=500, ignore_conflicts=True)
        created = len(to_create)

    if to_update:
        RagChunk.objects.bulk_update(
            to_update,
            fields=["doc_id", "url", "title", "text", "meta", "embedding", "dim"],
            batch_size=500,
        )
        updated = len(to_update)

    return {"saved": created + updated, "created": created, "updated": updated}
