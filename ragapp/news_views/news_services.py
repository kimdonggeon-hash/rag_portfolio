# ragapp/news_views/news_services.py

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse
import os

# 실제 구현은 서비스 계층 한 곳에서 관리
from ragapp.services import news_services as _svc
from ragapp.qa_data import get_faq_candidates

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 1. 기본 기능은 전부 services.news_services 그대로 래핑
# ─────────────────────────────────────────────────────────────

ask_gemini = _svc.ask_gemini
_embed_texts = _svc._embed_texts

fetch_article_text = _svc.fetch_article_text
crawl_news_bodies = _svc.crawl_news_bodies
search_news_rss = _svc.search_news_rss
gemini_answer_with_news = _svc.gemini_answer_with_news

indexto_chroma_safe = _svc.indexto_chroma_safe
chroma_upsert = _svc.chroma_upsert

_chunk_text = _svc._chunk_text
_slug = _svc._slug
_sha = _svc._sha
_iso = _svc._iso


# ─────────────────────────────────────────────────────────────
# 2. FAQ 후보를 hits에 항상 붙이고,
#    "답이 약할 때만" 메인 답변을 FAQ로 교체하는 헬퍼
# ─────────────────────────────────────────────────────────────

def _attach_faq_hits(question: str, hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """RAG hits 리스트에 FAQ 후보들을 추가 (중복은 제거)."""
    try:
        faq_cands = get_faq_candidates(question, top_k=3)
    except Exception as e:
        log.warning("FAQ 후보 조회 실패: %s", e)
        return hits

    if not faq_cands:
        return hits

    merged: List[Dict[str, Any]] = list(hits or [])
    seen = set()

    # 이미 포함된 FAQ 소스는 중복 방지
    for h in merged:
        m = h.get("meta") or {}
        if (m.get("source") == "faq") or (m.get("source_name") == "faq"):
            key = ((m.get("title") or ""), (h.get("snippet") or ""))
            seen.add(key)

    for cand in faq_cands:
        title = f"[FAQ] {cand.get('q', '')}"
        snippet = cand.get("a", "") or ""
        key = (title, snippet)
        if key in seen:
            continue
        seen.add(key)

        merged.append(
            {
                "meta": {
                    "title": title,
                    "source_name": "faq",
                    "source": "faq",
                    "url": "",
                },
                "snippet": snippet,
                "score": cand.get("score"),
            }
        )

    return merged


def _is_weak_answer(text: str) -> bool:
    """
    '모델 답변이 별로'일 때만 FAQ로 덮어쓰기 위해 약한 답변을 판별.
    - 거의 비었거나 너무 짧은 경우
    - 에러/폴백 느낌의 문구가 포함된 경우
    """
    t = (text or "").strip()
    if not t:
        return True
    if len(t) < 80:  # 한두 문장 수준이면 약한 답변으로 취급
        return True

    lower = t.lower()
    bad_markers = [
        "모델 호출 실패",
        "응답이 비었습니다",
        "프로젝트/리전/권한/모델명",
        "api가 답변을 반환하지 않았습니다",
    ]
    for m in bad_markers:
        if m in t:
            return True

    # 평범한 RAG 답변(4~8문장)은 대부분 120자 이상이므로 여기까지 오면 괜찮다고 봄
    return False


def _maybe_override_with_faq_answer(question: str, answer_text: str) -> str:
    """
    질문이 FAQ 질문이랑 '거의 똑같을 때만' FAQ 답변으로 메인 답변을 교체.
    그 외에는 RAG 답변을 그대로 둔다.
    """
    try:
        faq_best_list = get_faq_candidates(question, top_k=1)
    except Exception:
        return answer_text

    if not faq_best_list:
        return answer_text

    best = faq_best_list[0]
    faq_q = (best.get("q") or "").strip()
    faq_a = (best.get("a") or "").strip()
    if not faq_a:
        return answer_text

    # 점수(0~1 정도라고 가정)
    try:
        score = float(best.get("score", 0.0))
    except Exception:
        score = 0.0

    # 공백/기호 제거해서 비교
    import re

    def _norm(s: str) -> str:
        s = re.sub(r"[\s\r\n\t]+", "", s or "")
        s = re.sub(r"[!?~.,;:·…]+", "", s)
        return s

    q_norm = _norm(question)
    fq_norm = _norm(faq_q)
    if not q_norm or not fq_norm:
        return answer_text

    len_q = len(q_norm)
    len_fq = len(fq_norm)
    len_min = min(len_q, len_fq)
    len_max = max(len_q, len_fq)

    # 1) 완전 동일하면 무조건 FAQ 사용
    if q_norm == fq_norm and score >= 0.5:
        return faq_a

    # 2) 길이도 거의 같고(80% 이상), 한쪽이 다른 쪽을 거의 그대로 포함할 때만 허용
    if (
        score >= 0.9
        and len_min / max(len_max, 1) >= 0.8
        and (q_norm in fq_norm or fq_norm in q_norm)
    ):
        return faq_a

    # 그 외에는 RAG 답변 유지
    return answer_text



# ─────────────────────────────────────────────────────────────
# 3. RAG 답변 래퍼: FAQ를 '강하게 섞되', RAG를 기본으로 유지
# ─────────────────────────────────────────────────────────────

def rag_answer_grounded(
    question: str,
    initial_topk: int = 5,
    fallback_topk: int = 12,
    max_sources: int = 8,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    기본 RAG 검색/생성은 services.news_services.rag_answer_grounded 를 그대로 사용하고,
    그 위에 FAQ 후보를 hits에 추가.
    - 모델 답변은 기본적으로 RAG 결과를 그대로 사용
    - 단, 모델 답변이 “약한 경우”에만 FAQ 답변으로 교체
    """
    answer_text, hits = _svc.rag_answer_grounded(
        question,
        initial_topk=initial_topk,
        fallback_topk=fallback_topk,
        max_sources=max_sources,
    )

    # hits에 FAQ 소스 추가
    hits = _attach_faq_hits(question, hits)
    # 답변이 약할 때만 FAQ로 교체
    answer_text = _maybe_override_with_faq_answer(question, answer_text)

    return answer_text, hits


def rag_answer_grounded_with_history(
    question: str,
    history: List[dict],
    *,
    initial_topk: int = 5,
    fallback_topk: int = 12,
    max_sources: int = 8,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    히스토리 기반 RAG도 동일하게 FAQ를 섞어 줌.
    - 내부 검색/생성은 services.news_services.rag_answer_grounded_with_history 사용
    - RAG 답변을 기본으로 두고, 약할 때만 FAQ로 덮어씀
    """
    answer_text, hits = _svc.rag_answer_grounded_with_history(
        question,
        history,
        initial_topk=initial_topk,
        fallback_topk=fallback_topk,
        max_sources=max_sources,
    )

    # services.news_services 쪽에서도 FAQ를 붙일 수 있지만,
    # 여기서 한 번 더 합쳐도 크게 문제는 없음.
    hits = _attach_faq_hits(question, hits)
    answer_text = _maybe_override_with_faq_answer(question, answer_text)

    return answer_text, hits


# ─────────────────────────────────────────────────────────────
# 4. 프런트에서 쓰는 라벨 함수 (기존 구현 유지)
# ─────────────────────────────────────────────────────────────

def source_label(meta: Dict[str, Any]) -> str:
    title = (meta.get("title") or meta.get("url") or "문서").strip()
    src = (meta.get("source_name") or meta.get("source") or "").strip()
    u = (meta.get("url") or "").strip()

    bits = [title]
    if src:
        bits.append(src)
    if u:
        bits.append(u)

    return " · ".join(bits)


__all__ = [
    # 생성/임베딩
    "ask_gemini",
    "_embed_texts",

    # 크롤/검색
    "fetch_article_text",
    "crawl_news_bodies",
    "search_news_rss",
    "gemini_answer_with_news",

    # 인덱싱/스토어(호환)
    "indexto_chroma_safe",
    "chroma_upsert",

    # RAG
    "rag_answer_grounded",
    "rag_answer_grounded_with_history",

    # 프런트 라벨
    "source_label",

    # 헬퍼
    "_chunk_text",
    "_slug",
    "_sha",
    "_iso",
]

# ragapp/news_views/news_services.py 맨 아래쪽에 추가

from typing import Any, Dict, List, Optional
from django.conf import settings

def run_rag_qa(
    question: str,
    *,
    history_list: Optional[List[Dict[str, Any]]] = None,
    initial_topk: Optional[int] = None,
    fallback_topk: Optional[int] = None,
    max_sources: Optional[int] = None,
) -> Dict[str, Any]:
    """
    고수준 RAG 헬퍼.
    - rag_qa_view / 관리자 콘솔 등에서 공통으로 사용.
    - 반환 형식: {"answer": str, "sources": List[dict], "raw": Any}
    """

    q = (question or "").strip()
    if not q:
        return {"answer": "", "sources": [], "raw": None}

    hist = history_list or []

    topk = initial_topk if initial_topk is not None else max(
        1,
        int(getattr(settings, "RAG_QUERY_TOPK", 5)),
    )
    fb_topk = fallback_topk if fallback_topk is not None else max(
        topk + 5,
        int(getattr(settings, "RAG_FALLBACK_TOPK", 12)),
    )
    max_src = max_sources if max_sources is not None else int(
        getattr(settings, "RAG_MAX_SOURCES", 8)
    )

    # 👉 여기서 news_services.py 안에 이미 있는
    #    rag_answer_grounded_with_history / rag_answer_grounded 를 그대로 사용
    res = rag_answer_grounded_with_history(
        q,
        hist,
        base_retriever_func=rag_answer_grounded,
        initial_topk=topk,
        fallback_topk=fb_topk,
        max_sources=max_src,
    )

    if isinstance(res, tuple) and len(res) >= 2:
        rag_text, used_hits = res[0], res[1]
    elif isinstance(res, dict):
        rag_text = res.get("answer") or res.get("text") or ""
        used_hits = res.get("hits") or res.get("sources") or []
    else:
        rag_text = str(res)
        used_hits = []

    hits_payload: List[Dict[str, Any]] = []
    for i, h in enumerate(used_hits or [], start=1):
        if isinstance(h, dict):
            m = h.get("meta") or {}
            hits_payload.append(
                {
                    "idx": i,
                    "title": (
                        m.get("title")
                        or m.get("url")
                        or h.get("title")
                        or h.get("url")
                        or "문서"
                    ),
                    "source": (
                        m.get("source_name")
                        or m.get("source")
                        or h.get("source")
                        or ""
                    ),
                    "url": m.get("url") or h.get("url") or "",
                    "snippet": h.get("snippet") or "",
                    "score": m.get("score") if "score" in m else h.get("score"),
                }
            )
        else:
            hits_payload.append(
                {
                    "idx": i,
                    "title": str(h),
                    "source": "",
                    "url": "",
                    "snippet": "",
                    "score": None,
                }
            )

    return {
        "answer": rag_text,
        "sources": hits_payload,
        "raw": res,
    }
