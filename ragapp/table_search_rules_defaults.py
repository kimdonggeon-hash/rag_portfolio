# ragapp/table_search_rules_defaults.py
from __future__ import annotations

from typing import Dict, List

# 질문에서 집계 의도 읽을 때 쓰는 키워드들
DEFAULT_AGG_HINTS: Dict[str, List[str]] = {
    "sum":  ["합계", "총 ", "전체", "총액", "총매출", "total"],
    "avg":  ["평균", "평균적으로", "average", "avg"],
    "max":  ["최대", "가장 큰", "제일 큰", "가장 높은", "top"],
    "min":  ["최소", "가장 작은", "가장 낮은"],
    "count": ["개수", "건수", "몇 개", "몇개", "몇 명", "몇명", "row 수"],
}

# 컬럼 의미에 대한 한국어/영어 별칭
DEFAULT_COLUMN_SYNONYMS: Dict[str, List[str]] = {
    "region":  ["지역", "지역별", "도시", "시도", "branch", "지점"],
    "product": ["상품", "메뉴", "제품", "메뉴명", "item"],
    "channel": ["채널", "판매채널", "판매 경로", "sales channel"],
    "date":    ["날짜", "일자", "date", "일별"],
    "sales":   ["매출", "매출액", "금액", "판매금액", "revenue", "sales"],
}

# 숫자 컬럼 후보를 찾을 때 참고하는 이름 조각
DEFAULT_NUMERIC_HINTS: List[str] = [
    "sales", "amount", "revenue", "price", "qty", "quantity", "count",
]

# 벡터 유사도 기준 기본값
DEFAULT_MIN_SIMILARITY: float = 0.35

# 질문 내 값으로 하드 필터 적용 여부 기본값
DEFAULT_HARD_FILTER_ENABLED: bool = True
