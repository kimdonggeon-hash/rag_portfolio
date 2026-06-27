# ragapp/services/ragchunk_orm.py

from __future__ import annotations

import hashlib
from urllib.parse import urlparse

import numpy as np
from django.db import transaction

from ragapp.models import RagChunk


def _safe_url(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    try:
        p = urlparse(u)
        if p.scheme in ("http", "https") and p.netloc:
            return u
    except Exception:
        pass
    return ""


def save_ragchunks(
    ids: list[str],
    docs: list[str],
    metas: list[dict],
    embs,
    *,
    batch_size: int = 500,
) -> int:
    """
    벡터 업서트와 별개로, Django DB의 RagChunk 테이블에도 “조회용 스냅샷”을 저장합니다.
    - unique_hash = sha256(doc_id) 로 고정(중복 방지)
    - bulk_create(ignore_conflicts=True)라서 기존 레코드는 갱신하지 않습니다. (가벼운 1차 버전)
    """
    if not ids or not docs:
        return 0

    rows: list[RagChunk] = []
    for doc_id, text, meta, emb in zip(ids, docs, metas, embs):
        if not isinstance(text, str) or not text.strip():
            continue

        meta = meta or {}

        # ✅ 너무 얇은 청크 / 메타 전용 청크는 RagChunk 저장 제외
        # 검색 후보 품질 저하 방지용
        text_clean = text.strip()
        title_raw = str(meta.get("title") or "")
        source_raw = str(meta.get("source") or meta.get("source_name") or "")

        if "[META ONLY]" in text_clean.upper():
            continue
        if "[META ONLY]" in title_raw.upper():
            continue
        if len(text_clean) < 30:
            continue

        h = hashlib.sha256((doc_id or "").encode("utf-8")).hexdigest()

        title = (meta.get("title") or meta.get("source") or "")[:500]
        url = _safe_url(meta.get("url") or "")

        vec = np.asarray(emb, dtype=np.float32)

        # ✅ 비정상 임베딩은 저장 제외
        if vec.size <= 0:
            continue

        rows.append(
            RagChunk(
                unique_hash=h,
                doc_id=(doc_id or "")[:191],
                url=url,
                title=title,
                text=text_clean,
                meta=meta,
                embedding=vec.tobytes(),
                dim=int(vec.size),
            )
        )

    if not rows:
        return 0

    # DB 부담 줄이려고 bulk로 쪼개서 저장
    inserted = 0
    with transaction.atomic():
        for i in range(0, len(rows), batch_size):
            chunk = rows[i : i + batch_size]
            RagChunk.objects.bulk_create(chunk, ignore_conflicts=True, batch_size=batch_size)
            inserted += len(chunk)

    return inserted
