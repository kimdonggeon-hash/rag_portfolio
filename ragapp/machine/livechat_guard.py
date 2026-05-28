# ragapp/machine/livechat_guard.py
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

from django.conf import settings

from ragapp.utils.pii_guard import detect_pii  # 네가 이미 쓰는 PII 검사

@dataclass(frozen=True)
class GuardResult:
    blocked: bool
    reason: str
    kind: str  # "pii" | "abuse" | "sexual" | "guard_error" 등

_DEFAULT_ABUSE_PATTERNS = [
    r"(씨\s*발|ㅅ\s*ㅂ)",
    r"(병\s*신)",
    r"(개\s*새\s*끼)",
    r"(좆)",
    r"(멍청(하|해))",   # 네가 예시로 든 모욕
]

_DEFAULT_SEXUAL_PATTERNS = [
    r"(성희롱)",
    r"(섹스)",
    r"(야동)",
]

def _compile(patterns: list[str]) -> list[re.Pattern]:
    out = []
    for p in patterns:
        try:
            out.append(re.compile(p, re.IGNORECASE))
        except Exception:
            continue
    return out

_ABUSE_RE = _compile(getattr(settings, "LIVECHAT_ABUSE_PATTERNS", _DEFAULT_ABUSE_PATTERNS))
_SEXUAL_RE = _compile(getattr(settings, "LIVECHAT_SEXUAL_PATTERNS", _DEFAULT_SEXUAL_PATTERNS))

def guard_livechat_text(text: str) -> GuardResult:
    """
    상담 사용자 메시지 정책 검사.
    - PII: detect_pii 기반
    - 욕설/모욕/성희롱: 간단한 정규식(설정으로 확장 가능)
    """
    t = (text or "").strip()
    if not t:
        return GuardResult(False, "", "")

    # 1) PII (직원 보호 관점: 여기서는 fail-closed 권장)
    try:
        hit = detect_pii(t)
        blocked = False
        kind: Optional[str] = None

        if isinstance(hit, bool):
            blocked = hit
        elif isinstance(hit, (tuple, list)) and hit:
            blocked = bool(hit[0])
            if len(hit) >= 2 and hit[1] is not None:
                kind = str(hit[1])
        elif isinstance(hit, dict):
            blocked = bool(hit.get("hit") or hit.get("blocked") or hit.get("is_hit"))
            kind = hit.get("kind") or hit.get("type") or hit.get("pii_kind")
        else:
            blocked = bool(getattr(hit, "hit", False) or getattr(hit, "blocked", False) or getattr(hit, "is_hit", False))
            kind = getattr(hit, "kind", None) or getattr(hit, "type", None) or getattr(hit, "pii_kind", None)

        if blocked:
            return GuardResult(True, "개인정보로 보이는 내용은 상담사에게 전달되지 않습니다.", f"pii:{kind or ''}".strip(":"))
    except Exception:
        # 상담은 직원 보호가 우선이라: PII 가드가 터지면 전송 막는 편이 안전
        return GuardResult(True, "보안 정책상 해당 메시지는 전송되지 않습니다.", "guard_error")

    # 2) 성희롱 키워드/패턴
    for rx in _SEXUAL_RE:
        if rx.search(t):
            return GuardResult(True, "성희롱/부적절한 성적 표현은 전송되지 않습니다.", "sexual")

    # 3) 욕설/모욕
    for rx in _ABUSE_RE:
        if rx.search(t):
            return GuardResult(True, "욕설/모욕 표현은 전송되지 않습니다.", "abuse")

    return GuardResult(False, "", "")
