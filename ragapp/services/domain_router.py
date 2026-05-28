# ragapp/services/domain_router.py
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class DomainDecision:
    """
    domain:  "medical" | "law" | "person"
    action:  "clarify" | "allow" | "block"
    ask:     action == "clarify" 일 때 사용
    message: action == "block"  일 때 사용
    """
    domain: str
    action: str
    ask: str = ""
    message: str = ""


# -------------------------------
# 1) 2턴 합성 프롬프트(추가질문+사용자답변)인지 감지
# -------------------------------
_SECOND_TURN_MARKERS = ("[추가정보]", "[추가질문]", "[사용자답변]")


def is_second_turn_prompt(q: str) -> bool:
    s = (q or "")
    return any(m in s for m in _SECOND_TURN_MARKERS)


# -------------------------------
# 2) 도메인 키워드(가볍고 보수적으로)
# -------------------------------
_MED_RE = re.compile(
    r"(증상|진단|병원|약(물)?|복용|부작용|통증|열|기침|호흡|두통|복통|설사|구토|혈압|당뇨|임신|우울|불안|공황|치료|처방)",
    re.IGNORECASE,
)

_LAW_RE = re.compile(
    r"(고소|고발|소송|합의|민사|형사|처벌|벌금|검찰|경찰|변호사|판례|증거|계약|위약금|손해배상|내용증명|고지서|명예훼손|모욕)",
    re.IGNORECASE,
)

# ✅ 인물 “일반” 신호: 여기에는 루머/사생활 단어를 넣지 않음
_PERSON_GENERAL_RE = re.compile(
    r"(연예인|유명인|인물|정치인|선수|가수|배우|작가|유튜버|CEO|대표|정체|실명|누구(야|임)|누군지)",
    re.IGNORECASE,
)

# ✅ 루머/사생활/평판성 공격/확인불가 이슈: 아예 차단
_PERSON_RUMOR_PRIVACY_RE = re.compile(
    r"(논란|루머|소문|찌라시|사생활|폭로|불륜|바람|스캔들|열애|연애|결별|이혼|사망설|근황|학폭|전과|병력|재산\s*규모|재산\s*얼마|평판|헛소리|카더라)",
    re.IGNORECASE,
)

# ✅ 개인정보 직접 요구(하드): 아예 차단
_PERSON_PRIVATE_HARD_RE = re.compile(
    r"(주소|집\s*주소|직장\s*주소|어디\s*살아|전화번호|연락처|주민번호|계정\s*아이디|SNS\s*계정|인스타|카톡|카카오톡|dm|번호|위치추적|실시간\s*위치)",
    re.IGNORECASE,
)


def decide_domain(q: str) -> Optional[DomainDecision]:
    """
    - 의료/법/인물 도메인 신호가 뚜렷하면: 1회 추가질문(clarify) 유도
    - 2턴(추가질문+사용자답변 합쳐진 프롬프트)에서는 반복 clarify 방지: None 반환
    - ✅ 인물 루머/사생활/개인정보는 아예 block
    """
    s = (q or "").strip()
    if not s:
        return None

    # ✅ 2턴에서는 다시 clarify 걸지 않음(무한 루프 방지)
    if is_second_turn_prompt(s):
        return None

    # ✅ 인물: 개인정보/사생활/루머 계열은 아예 다루지 않음 → block
    if _PERSON_PRIVATE_HARD_RE.search(s) or _PERSON_RUMOR_PRIVACY_RE.search(s):
        return DomainDecision(
            domain="person",
            action="block",
            message="사생활·루머·개인정보 관련 내용은 다루지 않아요. 다른 주제로 질문해 주세요.",
        )

    if _MED_RE.search(s):
        return DomainDecision(
            domain="medical",
            action="clarify",
            ask=(
                "의료 질문은 진단/처방이 아니라 일반 정보로만 정리할게요. "
                "연령대(예: 20대/30대), 증상 지속 기간, 그리고 지금 가장 불편한 증상 1가지를 알려주세요."
            ),
        )

    if _LAW_RE.search(s):
        return DomainDecision(
            domain="law",
            action="clarify",
            ask=(
                "법률 질문은 일반 정보로만 정리할게요. "
                "어느 나라/지역 기준인지(예: 한국)와, 상황이 '이미 발생'인지 '예방/대비'인지 먼저 알려주세요."
            ),
        )

    # ✅ 인물 “일반 정보”는 가능하되, 공개적으로 확인 가능한 범위로 정리하도록 clarify
    if _PERSON_GENERAL_RE.search(s):
        return DomainDecision(
            domain="person",
            action="clarify",
            ask=(
                "인물 관련 질문은 공개적으로 확인 가능한 사실(공식 발표/공식 프로필/검증 가능한 기사)만 다룰게요. "
                "어떤 인물에 대해, 어떤 ‘공식 정보’(예: 소속/직책/이력/작품/수상 등) 중 무엇을 알고 싶은지 구체화해 주세요."
            ),
        )

    return None
