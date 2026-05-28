from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class GuardHit:
    code: str        # abuse | sexual
    detail: str      # 내부 로그용(짧게)
    user_msg: str    # 사용자 메시지(프론트/유저 노출)


# =========================
# 1) 욕설/모욕 (기존 유지)
# =========================

_ABUSE_PATTERNS = [
    r"병\s*신", r"ㅂ\s*ㅅ", r"ㅄ",
    r"시\s*발", r"씨\s*발", r"ㅅ\s*ㅂ", r"ㅆ\s*ㅂ",
    r"좆", r"존\s*나", r"지\s*랄", r"ㅈ\s*ㄹ",
    r"개\s*새\s*끼", r"새\s*끼",
    r"fuck", r"f\W*u\W*c\W*k",
]


# =========================
# 2) 성적 표현 (오탐 최소 + 문맥 탐지)
# =========================
# ✅ “확실한” 표현(직접/은어/우회) = 짧아도 즉시 차단
_SEXUAL_STRONG_PATTERNS = [
    # 기존(유지)
    r"섹\s*스",
    r"야\s*동",
    r"자\s*위",
    r"강\s*간",
    r"따\s*먹",

    # ✅ 직접 표현(오탐 적은 편)
    r"성\s*기",
    r"음\s*경",
    r"음\s*부",

    # ✅ 은어/우회(오탐 큰 것들은 제외하고 “확실한 쪽만”)
    r"자\s*지",
    r"보\s*지",
    r"ㅂ\s*ㅈ",
    r"ㅈ\s*ㅈ",
    r"꼬\s*추",   # '고추'는 음식 오탐이 커서 strong에 넣지 않음

    # ✅ 영문(특수문자/띄어쓰기 우회 포함)
    r"p\W*e\W*n\W*i\W*s",
    r"c\W*o\W*c\W*k",
    r"d\W*i\W*c\W*k",
    r"p\W*u\W*s\W*s\W*y",
    r"v\W*a\W*g\W*i\W*n\W*a",
]

# ✅ “빗대는 일상어” = 단독 차단 금지 (오탐 방지)
#    단, 아래 성적 맥락과 가까이 붙으면 차단(문맥상 그거다)
_SEXUAL_EUPHEMISM_WEAK = [
    r"고\s*추",
    r"바\s*나\s*나",
    r"막\s*대\s*기",
]

# ✅ 성적 맥락(오탐 적은 단어 위주: ‘포르노/야동/음란/오랄/삽입’)
#    *의학/교육 문장까지 과하게 막지 않도록 “너무 넓은 단어”는 일부러 뺐음
_SEXUAL_CONTEXT = [
    r"야\s*동",
    r"포\s*르\s*노",
    r"음\s*란",
    r"오\s*랄",
    r"삽\s*입",
    r"자\s*위",
]

# =========================
# 3) 컴파일
# =========================

def _compile_union(patterns: list[str]) -> re.Pattern:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


_abuse_re = _compile_union(_ABUSE_PATTERNS)
_sexual_strong_re = _compile_union(_SEXUAL_STRONG_PATTERNS)
_sexual_weak_re = _compile_union(_SEXUAL_EUPHEMISM_WEAK)
_sexual_ctx_re = _compile_union(_SEXUAL_CONTEXT)

# ✅ 공백/기호를 쭉 제거해서 “우회 입력”에도 강하게
_strip_re = re.compile(
    r"[\s\.\,\-\_\(\)\[\]\{\}\'\"\:\;\!\?\~\`\\\/\|\@\#\$\%\^\&\*\+=]+",
    re.UNICODE,
)

# ✅ 약표현+맥락이 “가까이(0~12자)” 붙으면 문맥상 성적 표현으로 판단
#    (compact 기준이라 공백/기호 우회도 잡힘)
_SEXUAL_WEAK_WINDOW = 12
_sexual_weak_pair_re = re.compile(
    r"(?:"
    r"(?:(?:" + "|".join(f"(?:{p})" for p in _SEXUAL_EUPHEMISM_WEAK) + r")).{0," + str(_SEXUAL_WEAK_WINDOW) + r"}(?:(?:" + "|".join(f"(?:{p})" for p in _SEXUAL_CONTEXT) + r"))"
    r"|"
    r"(?:(?:" + "|".join(f"(?:{p})" for p in _SEXUAL_CONTEXT) + r")).{0," + str(_SEXUAL_WEAK_WINDOW) + r"}(?:(?:" + "|".join(f"(?:{p})" for p in _SEXUAL_EUPHEMISM_WEAK) + r"))"
    r")",
    re.IGNORECASE,
)

# ✅ “짧은데도 너무 노골적인 조합”은 놓치면 안 되니까 더 타이트(0~4자)도 하나 둠
#    (예: 고추야동 / 야동고추 같은 극단 조합)
_SEXUAL_WEAK_TIGHT_WINDOW = 4
_sexual_weak_pair_tight_re = re.compile(
    r"(?:"
    r"(?:(?:" + "|".join(f"(?:{p})" for p in _SEXUAL_EUPHEMISM_WEAK) + r")).{0," + str(_SEXUAL_WEAK_TIGHT_WINDOW) + r"}(?:(?:" + "|".join(f"(?:{p})" for p in _SEXUAL_CONTEXT) + r"))"
    r"|"
    r"(?:(?:" + "|".join(f"(?:{p})" for p in _SEXUAL_CONTEXT) + r")).{0," + str(_SEXUAL_WEAK_TIGHT_WINDOW) + r"}(?:(?:" + "|".join(f"(?:{p})" for p in _SEXUAL_EUPHEMISM_WEAK) + r"))"
    r")",
    re.IGNORECASE,
)

# =========================
# 4) “긴 문장” 기준(약패턴 적용 조건)
# =========================
# ✅ 약표현(고추/바나나/막대기 등)은 단독 오탐이 많으니,
#    - “긴 문장”이거나
#    - “짧아도 타이트 조합(0~4자)”일 때만
#    문맥 차단을 태움.
_LONG_SENTENCE_MIN_CHARS = 18   # compact 기준 문자수
_LONG_SENTENCE_MIN_WORDS = 4    # 공백 기준 단어수(영문/혼합에 도움)


def _is_long_sentence(q: str, compact: str) -> bool:
    # compact 길이 우선(우회/기호 제거 후 의미 밀도가 더 정확)
    if len(compact) >= _LONG_SENTENCE_MIN_CHARS:
        return True
    # 공백 단어수는 한국어에서 정확하진 않지만, 짧은 드립/짤막한 문장을 걸러내는 보조장치로 유용
    words = [w for w in q.split() if w]
    if len(words) >= _LONG_SENTENCE_MIN_WORDS:
        return True
    return False


# =========================
# 5) 메인: 차단 히트 탐지
# =========================

def detect_block_hit(text: str) -> Optional[GuardHit]:
    q = (text or "").strip()
    if not q:
        return None

    compact = _strip_re.sub("", q)

    # ---- (1) 성적 “확실” 표현: 짧아도 즉시 차단 ----
    if _sexual_strong_re.search(q) or _sexual_strong_re.search(compact):
        return GuardHit(
            code="sexual",
            detail="sexual_strong_pattern",
            user_msg=(
                "⚠️ 성희롱/음란/성적 모욕 표현은 즉시 차단됩니다. "
                "해당 표현을 삭제/수정 후 다시 시도해 주세요. "
                "반복 시 기간 제한될 수 있습니다."
            ),
        )

    # ---- (2) 성적 “빗대는 표현”: 긴 문장 or 타이트 조합일 때만 차단 ----
    # 2-1) 짧아도 ‘너무 노골적인 조합(0~4자)’이면 즉시 차단
    if _sexual_weak_pair_tight_re.search(compact):
        return GuardHit(
            code="sexual",
            detail="sexual_euphemism_tight_pair",
            user_msg=(
                "⚠️ 성적인 의미로 해석될 수 있는 표현이 포함되어 차단되었습니다. "
                "표현을 바꿔 다시 시도해 주세요. "
                "반복 시 기간 제한될 수 있습니다."
            ),
        )

    # 2-2) 그 외에는 “긴 문장”일 때만 문맥 차단을 태움(오탐 방지)
    if _is_long_sentence(q, compact):
        # (선택) 약표현/맥락 둘 다 존재할 때만 pair 검사(조금 더 빠르고 보수적)
        if (_sexual_weak_re.search(q) or _sexual_weak_re.search(compact)) and (_sexual_ctx_re.search(q) or _sexual_ctx_re.search(compact)):
            if _sexual_weak_pair_re.search(compact):
                return GuardHit(
                    code="sexual",
                    detail="sexual_euphemism_with_context",
                    user_msg=(
                        "⚠️ 성적인 의미로 해석될 수 있는 표현이 포함되어 차단되었습니다. "
                        "표현을 바꿔 다시 시도해 주세요. "
                        "반복 시 기간 제한될 수 있습니다."
                    ),
                )

    # ---- (3) 욕설/모욕: 기존처럼 차단 ----
    if _abuse_re.search(q) or _abuse_re.search(compact):
        return GuardHit(
            code="abuse",
            detail="abuse_pattern",
            user_msg="⚠️ 욕설/모욕 표현은 사용할 수 없습니다. 표현을 수정해 주세요.",
        )

    return None
