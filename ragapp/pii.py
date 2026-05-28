# ragapp/pii.py
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple, Optional

from django.conf import settings


EMAIL_RE = re.compile(r"\b[a-zA-Z0-9._%+\-]{1,64}@[a-zA-Z0-9.\-]{1,190}\.[a-zA-Z]{2,20}\b")
PHONE_RE = re.compile(r"\b01[016789][\-\s]?\d{3,4}[\-\s]?\d{4}\b")
RRN_RE = re.compile(r"\b\d{6}[\-\s]?[1-4]\d{6}\b")  # 주민번호 유사패턴(오탐 가능성 있음)

MAX_HITS_DEFAULT = 3


@dataclass(frozen=True)
class PIIHit:
    kind: str
    sample: str


def detect_pii(text: str, max_hits: Optional[int] = None) -> List[PIIHit]:
    if not text:
        return []
    max_hits = int(max_hits or getattr(settings, "PII_GUARD_MAX_HITS", MAX_HITS_DEFAULT))

    hits: List[PIIHit] = []

    def _add(kind: str, s: str):
        nonlocal hits
        if len(hits) >= max_hits:
            return
        sample = (s[:30] + "…") if len(s) > 30 else s
        hits.append(PIIHit(kind=kind, sample=sample))

    for m in EMAIL_RE.finditer(text):
        _add("email", m.group(0))
        if len(hits) >= max_hits:
            return hits

    for m in PHONE_RE.finditer(text):
        _add("phone", m.group(0))
        if len(hits) >= max_hits:
            return hits

    for m in RRN_RE.finditer(text):
        _add("rrn_like", m.group(0))
        if len(hits) >= max_hits:
            return hits

    return hits


def summarize_hits(hits: List[PIIHit]) -> str:
    if not hits:
        return ""
    kinds = ", ".join(sorted(set(h.kind for h in hits)))
    return f"개인정보로 보일 수 있는 패턴({kinds})이 감지되었습니다."


def guard_text(text: str) -> Tuple[bool, List[PIIHit]]:
    """
    returns (ok, hits)
    ok=True면 통과
    """
    if not getattr(settings, "PII_GUARD_ENABLED", True):
        return True, []
    hits = detect_pii(text)
    return (len(hits) == 0), hits
