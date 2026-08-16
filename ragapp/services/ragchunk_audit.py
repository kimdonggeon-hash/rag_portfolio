# ragapp/services/ragchunk_audit.py
from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Dict, Iterable, List, Optional

from django.utils import timezone

log = logging.getLogger(__name__)


def _truthy_env(name: str, default: str = "1") -> bool:
    raw = os.environ.get(name, default)
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "", "none", "null")


def _field_names(Model) -> set[str]:
    try:
        return {f.name for f in Model._meta.get_fields() if hasattr(f, "attname")}
    except Exception:
        return set()


def _pick_first(existing: set[str], candidates: Iterable[str]) -> Optional[str]:
    for c in candidates:
        if c in existing:
            return c
    return None


def _sha1(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8", errors="ignore")).hexdigest()


def build_ragchunk_kwargs(
    *,
    RagChunkModel,
    text: str,
    title: str = "",
    url: str = "",
    source: str = "audit",
    chunk_index: Optional[int] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    RagChunk 모델 필드명이 프로젝트마다 달라도,
    '존재하는 필드만' 골라 kwargs를 조립한다.
    """
    fields = _field_names(RagChunkModel)
    data: Dict[str, Any] = {}

    # 텍스트 본문
    k_text = _pick_first(fields, ("text", "chunk_text", "content", "document", "body"))
    if k_text:
        data[k_text] = text

    # 제목
    k_title = _pick_first(fields, ("title", "doc_title", "source_title", "name"))
    if k_title and title:
        data[k_title] = title

    # URL/링크
    k_url = _pick_first(fields, ("url", "source_url", "doc_url", "link"))
    if k_url and url:
        data[k_url] = url

    # 출처/타입
    k_source = _pick_first(fields, ("source", "source_type", "kind"))
    if k_source and source:
        data[k_source] = source

    # 청크 번호
    k_idx = _pick_first(fields, ("chunk_index", "idx", "order", "chunk_no"))
    if k_idx and chunk_index is not None:
        data[k_idx] = int(chunk_index)

    # 길이/토큰 추정(있으면 넣기)
    k_char = _pick_first(fields, ("char_len", "char_count", "length"))
    if k_char:
        data[k_char] = len(text or "")

    # 해시(중복 추적용)
    k_hash = _pick_first(fields, ("chunk_hash", "text_hash", "sha1"))
    if k_hash:
        base = f"{source}|{url}|{chunk_index}|{_sha1(text)}"
        data[k_hash] = _sha1(base)

    # The current RagChunk schema requires these fields even for audit-only
    # mirrors. The searchable embedding remains in the vector store; an empty
    # audit embedding makes the row valid without duplicating vector data.
    if "unique_hash" in fields:
        data["unique_hash"] = _sha1(
            f"{source}|{url}|{title}|{chunk_index}|{_sha1(text)}"
        )
    if "doc_id" in fields:
        data["doc_id"] = str((meta or {}).get("doc_id") or _sha1(f"{source}|{url}|{title}"))[:191]
    if "embedding" in fields:
        data["embedding"] = b""
    if "dim" in fields:
        data["dim"] = 0

    # created_at 류
    k_created = _pick_first(fields, ("created_at", "created", "created_on"))
    if k_created:
        data[k_created] = timezone.now()

    # 메타 JSON 필드가 있으면 보너스로 넣기
    k_meta = _pick_first(fields, ("meta", "metadata", "extra", "extra_json"))
    if k_meta and meta:
        data[k_meta] = meta

    return data


def save_ragchunks_safe(
    *,
    texts: List[str],
    title: str = "",
    url: str = "",
    source: str = "audit",
    base_meta: Optional[Dict[str, Any]] = None,
    enabled_env: str = "RAGCHUNK_AUDIT",
    batch_size: int = 500,
) -> int:
    """
    texts(청크 리스트)를 RagChunk에 bulk_create 한다.
    실패해도 본 인덱싱 흐름을 깨지 않도록 안전하게 0을 반환한다.
    """
    if not texts:
        return 0
    if not _truthy_env(enabled_env, "1"):
        return 0

    try:
        from ragapp.models import RagChunk  # 로컬 import(순환 방지)
    except Exception:
        log.exception("RagChunk model import failed")
        return 0

    created = 0
    try:
        objs = []
        for i, t in enumerate(texts):
            meta = dict(base_meta or {})
            kwargs = build_ragchunk_kwargs(
                RagChunkModel=RagChunk,
                text=t or "",
                title=title,
                url=url,
                source=source,
                chunk_index=i,
                meta=meta if meta else None,
            )
            if not kwargs:
                continue
            objs.append(RagChunk(**kwargs))

        if not objs:
            return 0

        RagChunk.objects.bulk_create(objs, batch_size=batch_size)
        created = len(objs)
        return created
    except Exception:
        log.exception("save_ragchunks_safe failed (but indexing continues)")
        return 0
