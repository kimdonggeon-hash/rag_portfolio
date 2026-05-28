# ragapp/services/media_db_keywords.py
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Type

from django.apps import apps
from django.db.models import Q


_STOP = {
    "a", "an", "the", "of", "to", "in", "on", "for", "with", "and", "or",
    "is", "are", "was", "were",
    "사진", "이미지", "그림", "검색", "찾아줘", "관련", "같은", "느낌", "비슷한",
}

_TEXT_FIELD_CANDIDATES = [
    "search_text",
    "caption",
    "title",
    "name",
    "original_name",
    "orig_name",
    "basename",
    "filename",
    "file_name",
    "tags_text",
    "tags",
]

_KEY_FIELD_CANDIDATES = [
    "storage_key",
    "path",
    "filepath",
    "key",
]

_ID_FIELD_CANDIDATES = [
    "chroma_id",
    "pid",
]


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def _extract_phrase(q: str) -> Optional[str]:
    m = re.search(r'"([^"]+)"', q or "")
    if m and m.group(1).strip():
        return _norm(m.group(1))
    m = re.search(r"'([^']+)'", q or "")
    if m and m.group(1).strip():
        return _norm(m.group(1))
    return None


def _tokenize(q: str, *, max_tokens: int = 8) -> List[str]:
    qn = _norm(q)
    parts = re.findall(r"[0-9a-zA-Z가-힣_\-]+", qn)
    out: List[str] = []
    seen = set()
    for t in parts:
        if len(t) < 2:
            continue
        if t in _STOP:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= max_tokens:
            break
    return out


def _model_fields(Model: Type[Any]) -> set[str]:
    names = set()
    for f in Model._meta.get_fields():
        n = getattr(f, "name", None)
        if isinstance(n, str) and n:
            names.add(n)
    return names


def _pick_media_model() -> Optional[Type[Any]]:
    """
    ragapp 앱 내부 모델 중,
    - storage_key/path류 필드가 있고
    - caption/search_text/tags류 텍스트 필드가 있는
    모델을 자동으로 고른다.
    """
    for Model in apps.get_models():
        if getattr(Model._meta, "app_label", "") != "ragapp":
            continue
        f = _model_fields(Model)

        has_key = any(k in f for k in _KEY_FIELD_CANDIDATES)
        has_text = any(t in f for t in _TEXT_FIELD_CANDIDATES)
        if has_key and has_text:
            return Model
    return None


def _first_existing_field(Model: Type[Any], candidates: List[str]) -> Optional[str]:
    f = _model_fields(Model)
    for c in candidates:
        if c in f:
            return c
    return None


def keyword_search_images_db(*, query: str, limit: int = 200) -> List[Dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []

    Model = _pick_media_model()
    if Model is None:
        return []

    key_field = _first_existing_field(Model, _KEY_FIELD_CANDIDATES)
    if not key_field:
        return []

    # 텍스트 필드들(존재하는 것만)
    fields = _model_fields(Model)
    text_fields = [f for f in _TEXT_FIELD_CANDIDATES if f in fields]

    if not text_fields:
        return []

    id_field = _first_existing_field(Model, _ID_FIELD_CANDIDATES)

    tokens = _tokenize(q, max_tokens=10)
    phrase = _extract_phrase(q)

    # OR 기반(너무 빡빡하게 AND하면 0건이 잘 나와서 폴백 목적에 부적합)
    qobj = Q()
    for tok in tokens:
        tok_q = Q()
        for tf in text_fields:
            tok_q |= Q(**{f"{tf}__icontains": tok})
        qobj |= tok_q

    if phrase:
        ph_q = Q()
        for tf in text_fields:
            ph_q |= Q(**{f"{tf}__icontains": phrase})
        qobj |= ph_q

    qs = Model.objects.filter(qobj)

    # 가능한 정렬 필드 선택
    if "updated_at" in fields:
        qs = qs.order_by("-updated_at")
    elif "modified_at" in fields:
        qs = qs.order_by("-modified_at")
    elif "created_at" in fields:
        qs = qs.order_by("-created_at")
    else:
        qs = qs.order_by("-id")

    rows = list(qs[: int(limit)])

    out: List[Dict[str, Any]] = []
    for r in rows:
        key = getattr(r, key_field, None)
        if not key:
            continue

        caption = None
        for cf in ("caption", "search_text", "title", "name", "original_name", "orig_name", "basename", "filename", "file_name", "tags_text"):
            if cf in fields:
                v = getattr(r, cf, None)
                if isinstance(v, str) and v.strip():
                    caption = v.strip()
                    break

        if not caption and "tags" in fields:
            tv = getattr(r, "tags", None)
            if isinstance(tv, str) and tv.strip():
                caption = tv.strip()
            elif isinstance(tv, list):
                caption = " ".join([str(x) for x in tv if x is not None]) or None

        chroma_id = getattr(r, id_field, None) if id_field else None

        out.append(
            {
                "storage_key": str(key).strip(),
                "caption": caption or "(캡션 없음)",
                "chroma_id": str(chroma_id).strip() if chroma_id is not None else "",
            }
        )

    return out
