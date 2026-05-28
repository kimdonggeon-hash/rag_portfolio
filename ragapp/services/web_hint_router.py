# ragapp/services/web_hint_router.py
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Dict, List
from django.utils import timezone


@dataclass(frozen=True)
class WebHintRule:
    code: str
    pattern: re.Pattern
    message: str


_RULES: List[WebHintRule] = [
    WebHintRule(
        code="WEB_MODE_RECOMMENDED_TIME_SENSITIVE",
        pattern=re.compile(r"(오늘|어제|방금|지금|현재|실시간|최신|최근|이번주|이번달)", re.I),
        message="최신 정보가 필요해 보여요. 위의 있는 **웹 검색** 모드로 검색해 주세요.",
    ),
    WebHintRule(
        code="WEB_MODE_RECOMMENDED_REALTIME_DATA",
        pattern=re.compile(r"(주가|환율|날씨|경기\s*일정|상영\s*시간표|예약\s*가능|가격)", re.I),
        message="실시간 데이터가 필요한 질문이라 AI 요약 검색 대신 위의 있는 **웹 검색** 모드가 더 정확해요.",
    ),
]

# ✅ 20xx / 20xx년 (문자열 중 여러 개 있어도 모두 잡음)
_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})\s*(?:년)?(?!\d)")


def _extract_years(s: str) -> List[int]:
    years = []
    for m in _YEAR_RE.finditer(s or ""):
        try:
            years.append(int(m.group(1)))
        except Exception:
            continue
    return years


def decide_web_hint(q: str) -> Optional[Dict[str, str]]:
    s = (q or "").strip()
    if not s:
        return None

    # (선택) 강제로 RAG 쓰고 싶을 때 우회 프리픽스
    if s.startswith("!") or s.lower().startswith("[rag]"):
        return None

    # ✅ 1) 현재/직전년도 자동 감지 (Django TIME_ZONE 기준)
    cur_year = timezone.localdate().year  # settings.TIME_ZONE(Asia/Seoul) 반영
    target_years = {cur_year, cur_year - 1}

    years_in_q = _extract_years(s)
    if any(y in target_years for y in years_in_q):
        y_hit = next(y for y in years_in_q if y in target_years)
        return {
            "code": "WEB_MODE_RECOMMENDED_LATEST_YEAR",
            "message": f"{y_hit}년처럼 최신 연도 기준 정보는 변동될 수 있어요. 위의 있는 **웹 검색** 모드로 확인해 주세요.",
        }

    # ✅ 2) 기존 키워드 룰
    for r in _RULES:
        if r.pattern.search(s):
            return {"code": r.code, "message": r.message}

    return None
