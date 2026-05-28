# ragapp/machine/web_safety_guard.py
from __future__ import annotations

import re
from django.http import JsonResponse

BLOCK_MSG = "이 요청에 대해서는 제공 할 수 가 없습니다"

# "정의/개발/정책" 성격이면 키워드가 있어도 과차단 방지(단, 실제 PII 형태면 예외 없이 차단)
_DEV_OR_POLICY_ALLOW_RE = re.compile(
    r"(정의|뜻|설명|개념|예시|포맷|형식|정규식|regex|validation|검증|마스킹|redact|가리기|"
    r"개인정보처리방침|프라이버시|동의|보관|삭제|정책|가이드)",
    re.IGNORECASE,
)

# 실제 PII 형태(들어오면 무조건 차단)
_PII_SHAPE_RE = re.compile(
    r"(\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|"      # email
    r"\b01[016789][-\s]?\d{3,4}[-\s]?\d{4}\b|"         # KR mobile
    r"\b0\d{1,2}[-\s]?\d{3,4}[-\s]?\d{4}\b|"           # KR landline
    r"\b\d{6}[-\s]?[1-4]\d{6}\b|"                      # RRN
    r"\b\d{5}\b)",                                      # KR postcode(5 digits)
    re.IGNORECASE,
)

# "요청/노출" 의도(이게 있어야 키워드 차단을 발동 — 웹모드 과차단 방지용)
_INTENT_RE = re.compile(
    r"(해줘|해주세요|써줘|작성해|만들어|만들어줘|요약해줘|정리해줘|공개해|유출|폭로|"
    r"찾아줘|알아내|알려줘|expose|leak|doxx|insult|defame|rumor)",
    re.IGNORECASE,
)

# 모욕/비방(“욕해줘/모욕해줘/악플 써줘” 같은 케이스)
_INSULT_RE = re.compile(
    r"(욕(해|해줘|써|써줘)|모욕(해|해줘)?|비방(해|해줘)?|악플|조롱|비하|멸칭|디스|까(줘|달라)|"
    r"insult|mock|abuse|harass)",
    re.IGNORECASE,
)

# 루머/허위사실/찌라시 “만들어/퍼뜨려” 계열
_RUMOR_RE = re.compile(
    r"(루머|소문|찌라시|허위사실|가짜뉴스|뒷담|스캔들|논란|"
    r"rumor|gossip|defamation|slander)",
    re.IGNORECASE,
)

# 사생활/신상/연락처/주소/이메일/우편번호 노출 의도
_PRIVACY_RE = re.compile(
    r"(사생활|신상|뒷조사|도xx|도싱|doxx|"
    r"연락처|전화번호|휴대폰|핸드폰|이메일|메일주소|주소|집주소|우편번호|주민번호|계좌번호|"
    r"private|personal\s*info|address|phone|email)",
    re.IGNORECASE,
)


def web_blocked_json() -> JsonResponse:
    payload = {
        "ok": True,
        "code": "SAFETY_BLOCKED",
        "mode": "safety_blocked",
        "answer_text": BLOCK_MSG,
        "msg": BLOCK_MSG,
        "message": BLOCK_MSG,
        "sources": [],
    }
    return JsonResponse(payload, status=200)


def is_web_safety_blocked(q: str) -> bool:
    s = (q or "").strip()
    if not s:
        return False

    # 1) 실제 PII 형태는 무조건 차단
    if _PII_SHAPE_RE.search(s):
        return True

    # 2) 개발/정책/정의 성격이면 키워드가 있어도 과차단 방지
    if _DEV_OR_POLICY_ALLOW_RE.search(s):
        return False

    # 3) 의도(요청/노출)가 없으면 차단 안 함(웹모드 죽는 걸 방지)
    if not _INTENT_RE.search(s):
        return False

    # 4) 의도 + 카테고리 키워드가 같이 오면 차단
    if _INSULT_RE.search(s):
        return True
    if _RUMOR_RE.search(s):
        return True
    if _PRIVACY_RE.search(s):
        return True

    return False
