# ragapp/services/rag_engine.py
# 화면/뷰 단에서 기존처럼 import 하더라도, 실제 구현은
# ragapp/services/news_services.py 로 연결되도록 얇은 프록시만 유지합니다.

from __future__ import annotations

import re
from typing import List, Dict, Tuple
from urllib.parse import urlparse
from django.conf import settings

# ── Vertex(google-genai/Vertex 라우팅) 텍스트 생성기
from ragapp.services.news_services import ask_gemini as vertex_generate_text
# ── 벡터 검색(Chroma 없어도 SQLite 백엔드로 자동 동작) - 1차 경로
from ragapp.services.news_services import _chroma_collection as _vector_collection
from ragapp.services.news_services import _chroma_query_with_embeddings as _vector_query
# ── 멀티 백엔드(“sqlite”, “chroma”) 통합 검색기 - 2차 폴백 경로
from ragapp.services.vector_store import multi_query_by_embedding

# ── news_services의 구현을 그대로 re-export (뷰/타 코드 호환)
from .news_services import (
    # 생성/임베딩
    ask_gemini,
    _embed_texts,
    # 크롤/검색
    fetch_article_text,
    crawl_news_bodies,
    search_news_rss,          # ← news_services 버전만 사용 (중복 import 제거)
    # 인덱싱/스토어
    indexto_chroma_safe,
    chroma_upsert,
    # RAG
    rag_answer_grounded as _orig_rag_answer_grounded,  # 기존 구현(호환용)
    rag_answer_grounded_with_history as _orig_rag_answer_with_history,  # 기존 구현(호환용)
    # 헬퍼(호환)
    _chunk_text,
    _slug,
    _sha,
    _iso,
    _chroma_collection,
    _chroma_query_with_embeddings,
)

# ─────────────────────
# 로컬 URL 추출기 (의존성 최소화)
# ─────────────────────
_URL_RAW = re.compile(r"(https?://[^\s<>\]\)\"']+)")
_URL_MD  = re.compile(r"\[[^\]]+\]\((https?://[^\s)]+)\)")

def _extract_links_from_text(text: str, max_n: int = 5) -> List[str]:
    if not text:
        return []
    urls: List[str] = []
    try:
        urls += _URL_MD.findall(text)
    except Exception:
        pass
    try:
        urls += _URL_RAW.findall(text)
    except Exception:
        pass
    out: List[str] = []
    seen = set()
    for u in urls:
        u = u.strip().rstrip(").,]")
        if not u.lower().startswith(("http://", "https://")):
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= max_n:
            break
    return out


def gemini_answer_with_news(question: str) -> tuple[str, List[Dict]]:
    """
    1) (Vertex 기반) 모델에게 답 생성
    2) 관련 뉴스 헤더 리스트를 가져옴 (본문 크롤링은 별도 단계)
    """
    prompt = (
        "한국어로 간결하고 최신성 있게 답하세요.\n"
        "가능하면 참고할만한 기사/자료의 URL을 3~5개 본문 하단에 적어 주세요.\n\n"
        f"[질문]\n{question}\n\n[답변]\n"
    )
    answer = (vertex_generate_text(prompt) or "").strip()

    # 최신 뉴스 헤더
    try:
        topk = int(getattr(settings, "NEWS_TOPK", 5))
    except Exception:
        topk = 5

    try:
        news = search_news_rss(question, topk) or []
    except Exception:
        news = []

    # 뉴스가 없으면 답변 내 URL이라도 노출
    if not news:
        urls = _extract_links_from_text(answer, max_n=5)
        news = [
            {
                "title": u,
                "url": u,
                "source": urlparse(u).netloc,
                "published_at": "",
                "snippet": "",
            }
            for u in urls
        ]

    # UI에서 "(응답 없음)"이 뜨지 않도록 폴백 보강
    if not answer:
        answer = "API가 답변을 반환하지 않았습니다."

    return answer, news


# ─────────────────────
# 내부: 통합 벡터 검색 헬퍼
#   1차: news_services._chroma_query_with_embeddings
#   2차: 에러 시 vector_store.multi_query_by_embedding 폴백
#       → 결과는 Chroma 스타일 dict 로 변환
# ─────────────────────
def _run_vector_query(text: str, topk: int, where):
    """
    text 를 기반으로 벡터 검색을 수행하고,
    Chroma query 결과와 동일한 구조의 dict 를 반환한다.
    """
    # 1) 기존 Chroma 경로 우선 시도
    try:
        col = _vector_collection()
        if col is not None:
            return _vector_query(col, text, topk, where=where)
    except Exception:
        # 예: WinError 123 같은 경로 문제 발생 시 조용히 폴백
        pass

    # 2) 폴백: 텍스트 → 임베딩 → multi_query_by_embedding
    try:
        vecs = _embed_texts([text]) or []
        if not vecs:
            raise RuntimeError("임베딩 결과 없음")
        q_vec = vecs[0]

        mres = multi_query_by_embedding(query_embedding=q_vec, k=topk)
        hits = mres.get("hits") or []

        docs = [[h.get("doc", "") for h in hits]]
        metas = [[h.get("meta") or {} for h in hits]]
        dists = [[h.get("distance") for h in hits]]
        ids   = [[(h.get("meta") or {}).get("doc_id") or "" for h in hits]]

        return {
            "documents": docs,
            "metadatas": metas,
            "distances": dists,
            "ids": ids,
        }
    except Exception:
        # 완전 실패 시에도 형태만은 유지
        return {
            "documents": [[]],
            "metadatas": [[]],
            "distances": [[]],
        }


# ─────────────────────
# RAG answer helpers
# ─────────────────────

def _parse_hits_from_res(res: Dict) -> List[Dict]:
    def _pick(v):
        return v[0] if (isinstance(v, list) and v and isinstance(v[0], list)) else (v or [])
    docs  = _pick(res.get("documents"))
    metas = _pick(res.get("metadatas"))
    ids   = _pick(res.get("ids")) if "ids" in res else [""] * len(docs)
    dists = _pick(res.get("distances"))
    hits = []
    for i, doc in enumerate(docs):
        if not doc:
            continue
        snip = ((doc[:800] if isinstance(doc, str) else str(doc)).replace("\n", " ").strip())
        m = metas[i] if i < len(metas) else {}
        score = None
        if dists and i < len(dists) and dists[i] is not None:
            try:
                score = float(dists[i])
            except Exception:
                score = None
        hits.append({
            "id": ids[i] if i < len(ids) else "",
            "score": score,
            "meta": m,
            "snippet": snip,
        })
    return hits


def _rank_and_dedupe_hits(hits: List[Dict], max_n: int = 8) -> List[Dict]:
    def key_of(h):
        m = h.get("meta") or {}
        return (
            (m.get("url") or "").strip().lower(),
            (m.get("title") or "").strip(),
            (h.get("snippet") or "")[:120],
        )
    def score_of(h):
        s = h.get("score")
        try:
            return float(s) if s is not None else 1e9
        except Exception:
            return 1e9
    seen = set()
    ordered = sorted(hits, key=score_of)  # 거리 낮을수록 가까움
    out: List[Dict] = []
    for h in ordered:
        k = key_of(h)
        if k in seen:
            continue
        seen.add(k)
        out.append(h)
        if len(out) >= max_n:
            break
    return out


def _build_source_block(hits: List[Dict]) -> str:
    lines = []
    for i, h in enumerate(hits, start=1):
        m = h.get("meta") or {}
        title  = (m.get("title") or m.get("url") or "문서").strip()
        source = (m.get("source_name") or m.get("source") or urlparse(m.get("url") or "").netloc or "").strip()
        snippet = (h.get("snippet") or "").strip()[:700]
        lines.append(f"[{i}] {title} · {source}\n{snippet}")
    return "\n\n".join(lines)


def _make_rag_prompt(question: str, source_block: str, hard: bool = False) -> str:
    if hard:
        return (
            "아래 '근거 자료'에 있는 내용만 사용해 한국어로 핵심을 정리해 답하세요.\n"
            "- 문장/항목 끝에 반드시 [1], [2]처럼 근거 번호 인용을 붙이세요.\n"
            "- 직접적 근거가 부족하면 '자료 내 직접 근거 부족' 한 줄만 쓰고 추측은 금지합니다.\n"
            "- 군더더기 없이 핵심만 요약하세요.\n\n"
            f"[질문]\n{question}\n\n[근거 자료]\n{source_block}\n\n=== 답변 시작 ===\n"
        )
    return (
        "아래 '근거 자료'를 최우선으로 참고해 한국어로 4~8문장으로 핵심을 답하세요.\n"
        "- 가능하면 문장 끝에 [1], [2]처럼 근거 번호를 붙이되, 직접 근거가 없으면 인용은 생략 가능합니다.\n"
        "- 근거가 부족한 부분은 일반 지식/상식으로 간결히 보완하세요(과도한 추측 금지).\n"
        "- 불필요한 서론 없이 핵심만.\n\n"
        f"[질문]\n{question}\n\n[근거 자료]\n{source_block}\n\n=== 답변 시작 ===\n"
    )


def search_similar_by_embedding(q_vec, k=8):
    res = multi_query_by_embedding(query_embedding=q_vec, k=k)
    hits = res["hits"]  # [{doc, meta, distance, backend}, ...]
    # 필요하면 hits를 근거 리스트로 변환
    evidences = []
    for h in hits:
        meta = h.get("meta") or {}
        title = meta.get("title") or "(제목 없음)"
        src = meta.get("url") or meta.get("source_name") or meta.get("source") or ""
        evidences.append(f"{title} {('· ' + src) if src else ''}")
    return hits, evidences


def rag_answer_grounded(
    question: str,
    initial_topk: int = 5,
    fallback_topk: int = 12,
    max_sources: int = 8,
) -> Tuple[str, List[Dict]]:
    """
    1) 현재 벡터 스토어에서 유사도 검색
       - 1차: news_services._chroma_query_with_embeddings (Chroma)
       - 2차: 에러 시 vector_store.multi_query_by_embedding 폴백
    2) 근거로 (Vertex 기반) 답 생성
    3) 부족하면 키워드 확장해서 한 번 더
    4) 그래도 부족하면(옵션) 일반 지식 fallback
    """
    where_filter_cfg = getattr(settings, "RAG_SOURCES_FILTER", None)

    # 1차: 직접 질의
    res = _run_vector_query(question, initial_topk, where_filter_cfg)
    hits = _rank_and_dedupe_hits(_parse_hits_from_res(res), max_sources)
    block = _build_source_block(hits)
    force_answer = getattr(settings, "RAG_FORCE_ANSWER", True)
    ans = (vertex_generate_text(_make_rag_prompt(question, block, hard=not force_answer)) or "").strip()

    def _weak(a: str) -> bool:
        t = (a or "").strip()
        return (not t) or (len(t) < 120)

    # 2차: 부족하면 키워드 확장
    if _weak(ans):
        try:
            kw = (vertex_generate_text(
                "아래 질문의 한국어 핵심 키워드를 쉼표로 10개만. 설명 없이 키워드만:\n" + question
            ) or "").strip()
        except Exception:
            kw = ""
        expanded_q = (question + " " + (kw or "")).strip()
        res2 = _run_vector_query(expanded_q, fallback_topk, None)
        hits2 = _rank_and_dedupe_hits(_parse_hits_from_res(res2), max_sources)
        if hits2:
            block2  = _build_source_block(hits2)
            ans2 = (vertex_generate_text(_make_rag_prompt(question, block2, hard=not force_answer)) or "").strip()
            if not _weak(ans2):
                return ans2, hits2
            # 아직 약하면 hits2/ans2 유지
            hits = hits2
            ans = ans2

    # 3차: 그래도 약하고 force_answer=True 라면 일반 지식 fallback
    if force_answer and _weak(ans):
        ans_fb = (vertex_generate_text(
            "다음 질문에 대해 일반 지식과 상식, 최신 경향을 바탕으로 "
            "한국어로 4~8문장 핵심 요약 답을 작성하세요. "
            "군더더기 금지, 안전하고 중립적인 표현 사용:\n\n"
            f"{question}\n\n=== 답변 시작 ===\n"
        ) or "").strip()
        if ans_fb:
            return ans_fb, hits

    # 최종 폴백 방지
    if not ans:
        ans = "API가 답변을 반환하지 않았습니다."
    return ans, hits


def rag_answer_grounded_with_history(
    question: str,
    history: list[dict],
    *,
    base_retriever_func = rag_answer_grounded,
    initial_topk: int = 5,
    fallback_topk: int = 12,
    max_sources: int = 8,
) -> Tuple[str, List[Dict]]:
    # 최근 turns의 사용자 질문을 단순 결합해 질의 강화
    hist_q = " ".join(t.get("q", "").strip() for t in (history or [])[-3:] if t.get("q"))
    aug_question = (question + " " + hist_q).strip() if hist_q else question

    # 지정된 베이스 검색/생성기 사용 (기본 rag_answer_grounded)
    answer_text, hits = base_retriever_func(
        aug_question,
        initial_topk=initial_topk,
        fallback_topk=fallback_topk,
        max_sources=max_sources,
    )
    if not answer_text:
        answer_text = "API가 답변을 반환하지 않았습니다."
    return answer_text, hits


__all__ = [
    # 생성/임베딩
    "ask_gemini",
    "_embed_texts",
    # 크롤/검색
    "fetch_article_text",
    "crawl_news_bodies",
    "search_news_rss",
    "gemini_answer_with_news",
    # 인덱싱/스토어
    "indexto_chroma_safe",
    "chroma_upsert",
    # RAG
    "rag_answer_grounded",
    "rag_answer_grounded_with_history",
    "search_similar_by_embedding",
    # 헬퍼(호환)
    "_chunk_text",
    "_slug",
    "_sha",
    "_iso",
    "_chroma_collection",
    "_chroma_query_with_embeddings",
]
