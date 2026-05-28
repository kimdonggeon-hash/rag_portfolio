# ragapp/services/ingest.py
from __future__ import annotations

from typing import Any, Dict, Optional


def indexto_chroma_safe(
    query: Optional[str] = None,
    answer: str = "",
    news: Optional[list] = None,
    *,
    # 하위호환 이름들
    question: Optional[str] = None,
    news_list: Optional[list] = None,
) -> Dict[str, Any]:
    """
    ✅ 단일화 목적의 호환 래퍼.
    - 실제 인덱싱은 news_services.indexto_chroma_safe(SQLite hybrid + MAX LEGAL-SAFE)로 위임
    - 예전 호출(query/news)과 새 호출(question/news_list) 모두 수용
    """
    q = (query or question or "").strip()
    n = news if news is not None else (news_list or [])

    # 지연 import로 순환/무거운 import 방지
    from ragapp.services import news_services as ns

    return ns.indexto_chroma_safe(
        question=q,
        answer=answer or "",
        news_list=n or [],
    )


__all__ = ["indexto_chroma_safe"]
