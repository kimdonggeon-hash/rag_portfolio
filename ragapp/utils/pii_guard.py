# ragapp/utils/pii_guard.py
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional


# =============================================================================
# Patterns
# =============================================================================

# 1) 한국 휴대폰 번호 (010/011/016/017/018/019) + 구분자 허용
PHONE_RE = re.compile(r"(?<!\d)(?:01[016789])[-\s]?\d{3,4}[-\s]?\d{4}(?!\d)")

# 1.5) 이메일 (PII로 차단)
EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])"
    r"[A-Za-z0-9._%+\-]+"
    r"@"
    r"(?:[A-Za-z0-9\-]+\.)+"
    r"[A-Za-z]{2,24}"
    r"(?![A-Za-z0-9._%+\-])"
)

# 2) 주민등록번호 (YYMMDD-XXXXXXX) / 외국인번호도 5~8 포함 가능
RRN_RE = re.compile(
    r"(?<!\d)"
    r"\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])"
    r"[-\s]?"
    r"[1-8]\d{6}"
    r"(?!\d)"
)

# 3) 주소(휴리스틱) - 도로명: "테헤란로 123", "강남대로12길 3-1" 등
ROAD_ADDR_RE = re.compile(
    r"(?:[가-힣A-Za-z0-9·\.\-]{2,})(?:로|길)\s*\d{1,4}(?:-\d{1,4})?"
)

# 4) 주소(휴리스틱) - 지번/행정동: "역삼동 123-4", "서초동 12"
LOT_ADDR_RE = re.compile(
    r"(?:[가-힣A-Za-z]{1,})(?:동|읍|면|리)\s*\d{1,4}(?:-\d{1,4})?"
)

# 4.5) 우편번호 5자리 (단독도 PII로 차단)
POSTCODE_RE = re.compile(r"(?<!\d)\d{5}(?!\d)")

# 5) 카드번호 후보: 13~19자리(구분자 허용) + Luhn로 확정
CARD_CAND_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")

# 6) 계좌번호 후보(전부 차단 모드): 10~20자리(구분자 허용)
ACCOUNT_CAND_RE = re.compile(r"(?<!\d)(?:\d[ -]?){10,20}(?!\d)")

# 전부 차단 모드(요청대로): True면 문맥 없이 계좌 후보(10~20자리)를 전부 차단
STRICT_BLOCK_ACCOUNTS = True


# =============================================================================
# Result type
# =============================================================================

@dataclass(frozen=True)
class PIIDetectResult:
    hit: bool
    kind: Optional[str] = None
    sample: Optional[str] = None  # ⚠️ 원문 유출 방지: 항상 마스킹/토큰만 넣기


# =============================================================================
# Helpers
# =============================================================================

def _digits_only(s: str) -> str:
    return re.sub(r"\D", "", s or "")

def _mask_last4(digits: str) -> str:
    if not digits:
        return "****"
    if len(digits) <= 4:
        return "****"
    return "****" + digits[-4:]

def _mask_phone(s: str) -> str:
    d = _digits_only(s)
    return _mask_last4(d)

def _mask_email(s: str) -> str:
    # 로그/응답 유출 방지 목적: 형태만 보여주고 내용은 숨김
    # 예: a***@domain.com
    try:
        local, domain = s.split("@", 1)
        if not local:
            return "***@" + domain
        return (local[0] + "***@" + domain) if domain else (local[0] + "***@***")
    except Exception:
        return "***@***"

def _luhn_ok(digits: str) -> bool:
    # digits: 숫자만
    total = 0
    rev = digits[::-1]
    for i, ch in enumerate(rev):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# =============================================================================
# Core detection
# =============================================================================

def detect_pii(text: str) -> PIIDetectResult:
    """
    문자열 1개에서 PII를 감지한다.
    - sample은 절대 원문을 담지 않는다(마스킹/토큰만).
    """
    t = (text or "").strip()
    if not t:
        return PIIDetectResult(False)

    m = PHONE_RE.search(t)
    if m:
        return PIIDetectResult(True, "전화번호", _mask_phone(m.group(0)))

    m = EMAIL_RE.search(t)
    if m:
        return PIIDetectResult(True, "이메일", _mask_email(m.group(0)))

    # RRN은 카드 후보(13~19자리)와 겹칠 수 있어 먼저 체크
    m = RRN_RE.search(t)
    if m:
        return PIIDetectResult(True, "주민등록번호(또는 유사 번호)", "[RRN]")

    # 카드번호: 후보 → 숫자만 → Luhn 통과 시 확정
    m = CARD_CAND_RE.search(t)
    if m:
        digits = _digits_only(m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return PIIDetectResult(True, "카드번호", _mask_last4(digits))

    # 계좌번호: 전부 차단 모드면 후보(10~20자리) 바로 차단
    m = ACCOUNT_CAND_RE.search(t)
    if m:
        digits = _digits_only(m.group(0))
        if 10 <= len(digits) <= 20 and STRICT_BLOCK_ACCOUNTS:
            return PIIDetectResult(True, "계좌번호(또는 유사 번호)", _mask_last4(digits))

    # 주소는 오탐 가능성이 있으니 "도로명/행정동 + 숫자" 형태만 잡는다
    m = ROAD_ADDR_RE.search(t)
    if m:
        return PIIDetectResult(True, "주소(도로명)", "[ADDRESS]")

    m = LOT_ADDR_RE.search(t)
    if m:
        return PIIDetectResult(True, "주소(지번/행정동)", "[ADDRESS]")

    m = POSTCODE_RE.search(t)
    if m:
        return PIIDetectResult(True, "우편번호", "[POSTCODE]")

    return PIIDetectResult(False)


# =============================================================================
# Nested detection (dict/list 전체 검사)
# =============================================================================

def _iter_strings(obj: Any) -> Iterable[str]:
    if obj is None:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                yield k
            yield from _iter_strings(v)
    elif isinstance(obj, (list, tuple, set)):
        for x in obj:
            yield from _iter_strings(x)

def detect_pii_any(obj: Any) -> PIIDetectResult:
    """
    dict/list 등 중첩 구조 전체에서 문자열을 훑어 PII를 감지한다.
    (전역 미들웨어/serializer validate에서 쓰기 좋음)
    """
    for s in _iter_strings(obj):
        r = detect_pii(s)
        if r.hit:
            return r
    return PIIDetectResult(False)


# =============================================================================
# Redaction (원문에서 PII를 토큰으로 치환)
# =============================================================================

def redact_pii(text: str) -> str:
    t = text or ""

    t = PHONE_RE.sub("[PHONE]", t)
    t = EMAIL_RE.sub("[EMAIL]", t)
    t = RRN_RE.sub("[RRN]", t)

    # 카드: Luhn 통과한 것만 [CARD]로 치환 (오탐 줄이기)
    def _card_repl(m: re.Match[str]) -> str:
        digits = _digits_only(m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return "[CARD]"
        return m.group(0)

    t = CARD_CAND_RE.sub(_card_repl, t)

    # 계좌: 전부 차단 모드면 후보(10~20자리)를 [ACCOUNT]로 치환
    def _acct_repl(m: re.Match[str]) -> str:
        digits = _digits_only(m.group(0))
        if 10 <= len(digits) <= 20 and STRICT_BLOCK_ACCOUNTS:
            return "[ACCOUNT]"
        return m.group(0)

    t = ACCOUNT_CAND_RE.sub(_acct_repl, t)

    t = ROAD_ADDR_RE.sub("[ADDRESS]", t)
    t = LOT_ADDR_RE.sub("[ADDRESS]", t)
    t = POSTCODE_RE.sub("[POSTCODE]", t)
    return t