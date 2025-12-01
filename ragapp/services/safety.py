# ragapp/services/safety.py

SENSITIVE_KEYWORDS = [
    "생일", "생년월일", "주민번호", "주민 등록 번호",
    "전화번호", "휴대폰 번호", "핸드폰 번호",
    "집 주소", "주소 뭐야", "계좌", "계좌번호",
    "비밀번호", "password",
    "이름 뭐야", "이름이 뭐야", "운영자의 이름", "운영자 이름",
    "대표 이름", "대표님 이름",
]

def is_sensitive_question(q: str) -> bool:
    """
    개인정보를 캐내려는/노출하려는 의도가 있는지 아주 단순하게 체크.
    향후 여기에 더 추가 가능.
    """
    low = q.lower()
    for kw in SENSITIVE_KEYWORDS:
        if kw.replace(" ", "") in low.replace(" ", ""):
            return True
    return False


def safe_block_response(q: str) -> str:
    """
    차단 시 사용자에게 돌려줄 기본 메시지.
    """
    return (
        "해당 내용은 개인 정보에 해당하여 안내할 수 없습니다."
    )
