from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.cache import cache

from ragapp.pii import guard_text, summarize_hits


DEFAULT_ABUSE_PATTERNS = [
    r"(씨\s*발|ㅅ\s*ㅂ)",
    r"(병\s*신)",
    r"(개\s*새\s*끼)",
    r"(좆)",
    r"(멍청(하|해))",
]
DEFAULT_SEXUAL_PATTERNS = [
    r"(성희롱)",
    r"(섹스)",
    r"(야동)",
]

_CACHE_KEY = "livechat_policy_v1"
_CACHE_TTL = 60  # seconds


@dataclass(frozen=True)
class BlockResult:
    ok: bool
    code: str
    message: str


def _compile(patterns: list[str]) -> list[re.Pattern]:
    out: list[re.Pattern] = []
    for p in patterns or []:
        try:
            out.append(re.compile(str(p), re.IGNORECASE))
        except Exception:
            continue
    return out


def _load_patterns() -> tuple[list[re.Pattern], list[re.Pattern]]:
    cached = cache.get(_CACHE_KEY)
    if cached:
        return cached

    abuse = list(getattr(settings, "LIVECHAT_ABUSE_PATTERNS", DEFAULT_ABUSE_PATTERNS) or [])
    sexual = list(getattr(settings, "LIVECHAT_SEXUAL_PATTERNS", DEFAULT_SEXUAL_PATTERNS) or [])

    # (옵션) DB 패턴을 붙이고 싶으면 model이 있을 때만 로드
    try:
        from ragapp.models import LiveChatBlockPattern  # type: ignore

        qs = LiveChatBlockPattern.objects.filter(enabled=True).values("kind", "pattern")  # type: ignore
        for row in qs:
            kind = (row.get("kind") or "").strip().lower()
            pat = (row.get("pattern") or "").strip()
            if not pat:
                continue
            if kind == "abuse":
                abuse.append(pat)
            elif kind == "sexual":
                sexual.append(pat)
    except Exception:
        pass

    compiled = (_compile(abuse), _compile(sexual))
    cache.set(_CACHE_KEY, compiled, _CACHE_TTL)
    return compiled


def _match_any(regexes: list[re.Pattern], text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    for rx in regexes:
        try:
            if rx.search(t):
                return True
        except Exception:
            continue
    return False


def check_livechat_text(text: str) -> BlockResult:
    """
    반환:
      ok=True  -> 통과
      ok=False -> 차단(code/message 제공)
    """
    t = str(text or "")

    # 1) PII
    ok, hits = guard_text(t)
    if not ok:
        msg = summarize_hits(hits) or "개인정보로 보이는 내용은 전송할 수 없습니다."
        return BlockResult(False, "PII_BLOCKED", msg)

    abuse_re, sexual_re = _load_patterns()

    # 2) 성희롱
    if _match_any(sexual_re, t):
        return BlockResult(False, "SEXUAL_BLOCKED", "성희롱/부적절한 성적 표현은 전송할 수 없습니다.")

    # 3) 욕설/모욕
    if _match_any(abuse_re, t):
        return BlockResult(False, "ABUSE_BLOCKED", "욕설/모욕 표현은 전송할 수 없습니다.")

    return BlockResult(True, "OK", "")


def should_end_on(code: str) -> bool:
    """
    consumers에서 ‘차단되면 즉시 종료’ 여부 제어(기본 True)
    """
    code = (code or "").upper()
    if code == "ABUSE_BLOCKED":
        return str(getattr(settings, "LIVECHAT_END_ON_ABUSE", True)).lower() in ("1", "true", "yes", "y", "on")
    if code == "SEXUAL_BLOCKED":
        return str(getattr(settings, "LIVECHAT_END_ON_SEXUAL", True)).lower() in ("1", "true", "yes", "y", "on")
    if code == "PII_BLOCKED":
        return str(getattr(settings, "LIVECHAT_END_ON_PII", True)).lower() in ("1", "true", "yes", "y", "on")
    return True
