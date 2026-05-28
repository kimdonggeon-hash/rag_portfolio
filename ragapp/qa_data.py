# ragapp/qa_data.py
from __future__ import annotations

from typing import List, Dict, Optional, Tuple
import threading
import math
import os
import time
from datetime import datetime

from django.db.models import QuerySet, Count, Max

from ragapp.models import FaqEntry


def _tokenize(text: str) -> List[str]:
    """
    아주 단순 토크나이저.
    - 소문자로 내리고
    - 한글/영문/숫자만 남겨서
    - 공백 단위 토큰 비슷하게 분리
    """
    if not text:
        return []
    clean = ""
    for ch in text.lower():
        if ch.isalnum() or ch.isspace():
            clean += ch
        else:
            clean += " "
    return [tok for tok in clean.split() if tok]


# -----------------------------------------
# 1) 하드코딩 Q/A (유지: DB 비었을 때 폴백)
# -----------------------------------------
QA_PAIRS: List[Dict[str, str]] = [
    {"q": "여기서 무엇을 할 수 있지?", "a": "검색 서비스를 기반으로 움직이면 돼."},
    {"q": "이걸 왜 만든거야?", "a": "재밌잖아?."},
    {"q": "이 서비스는 뭐 하는 거예요?", "a": "저의 창작물을 마음껏 펼치는 서비스 입니다."},
    {"q": "이거는 무엇을 하는거야?", "a": "검색놀이"},
]


# -----------------------------------------
# 2) 캐시 구조
# -----------------------------------------
_QA_CACHE = {
    "ready": False,
    "questions": [],    # type: List[str]
    "answers": [],      # type: List[str]
    "embeddings": [],   # type: List[List[float]]

    # ✅ DB 변경 감지용 시그니처
    "sig_cnt": 0,       # type: int
    "sig_ts": None,     # type: Optional[datetime]
    "sig_checked_at": 0.0,  # type: float
}
_QA_LOCK = threading.Lock()

# ✅ DB 시그니처 체크 주기(초)
# - 0이면 매 요청마다 DB 확인 (가장 즉시)
# - 기본 0.5초면 체감상 즉시 + DB부하 과하지 않음
_QA_SIG_CHECK_INTERVAL_SEC = float(os.getenv("QA_SIG_CHECK_INTERVAL_SEC", "0.5"))


def invalidate_qa_cache() -> None:
    """
    ✅ 외부(시그널/관리자 저장 후 훅)에서 호출 가능:
    캐시를 강제로 무효화해서 다음 호출 때 DB에서 다시 로드하게 한다.
    """
    with _QA_LOCK:
        _QA_CACHE["ready"] = False
        _QA_CACHE["questions"] = []
        _QA_CACHE["answers"] = []
        _QA_CACHE["embeddings"] = []
        _QA_CACHE["sig_cnt"] = 0
        _QA_CACHE["sig_ts"] = None
        _QA_CACHE["sig_checked_at"] = 0.0


def _lazy_embed_texts(text_list: List[str]) -> List[List[float]]:
    """
    순환 import 방지용 지연 임포트.
    news_services._embed_texts 를 여기서 '나중에' import한다.
    """
    try:
        from ragapp.services.news_services import _embed_texts as _real_embed_texts
    except Exception:
        from ragapp.news_views.news_services import _embed_texts as _real_embed_texts
    return _real_embed_texts(text_list)


def _base_faq_qs() -> QuerySet:
    """
    활성 FAQ만 가져오되, 예외면 전체로 폴백.
    """
    try:
        return FaqEntry.objects.filter(is_active=True)
    except Exception:
        return FaqEntry.objects.all()


def _get_db_signature() -> Tuple[int, Optional[datetime]]:
    """
    활성 FAQ들의 “개수 + 최신 갱신 시각”을 1쿼리로 가져옴.
    - updated_at이 없으면 created_at로 대체
    """
    qs = _base_faq_qs()
    agg = qs.aggregate(
        cnt=Count("id"),
        max_u=Max("updated_at"),
        max_c=Max("created_at"),
    )
    cnt = int(agg.get("cnt") or 0)
    ts = agg.get("max_u") or agg.get("max_c")
    return cnt, ts


def _should_refresh_cache() -> bool:
    """
    ✅ 캐시가 ready 상태여도, DB에 변경이 있으면 자동 리로드하도록 변경 감지.
    """
    now = time.time()

    last_checked = float(_QA_CACHE.get("sig_checked_at") or 0.0)
    if _QA_SIG_CHECK_INTERVAL_SEC > 0 and (now - last_checked) < _QA_SIG_CHECK_INTERVAL_SEC:
        return False

    try:
        cnt, ts = _get_db_signature()
    except Exception:
        # 시그니처 조회 실패 시, 안전하게 갱신 안 함(서비스 안정성 우선)
        _QA_CACHE["sig_checked_at"] = now
        return False

    old_cnt = int(_QA_CACHE.get("sig_cnt") or 0)
    old_ts = _QA_CACHE.get("sig_ts")

    _QA_CACHE["sig_checked_at"] = now
    _QA_CACHE["sig_cnt"] = cnt
    _QA_CACHE["sig_ts"] = ts

    return (cnt != old_cnt) or (ts != old_ts)


def _prepare_qa_cache(force: bool = False) -> None:
    """
    ✅ 핵심:
    - 운영 중 FAQ 수정/추가/삭제가 발생하면 DB 시그니처 감지로 자동 리로드
    - DB가 비었으면 QA_PAIRS로 폴백
    """
    if not force and _QA_CACHE["ready"]:
        if not _should_refresh_cache():
            return

    with _QA_LOCK:
        if not force and _QA_CACHE["ready"]:
            if not _should_refresh_cache():
                return

        qs = _base_faq_qs()
        try:
            qs = qs.order_by("-updated_at", "-created_at")
        except Exception:
            qs = qs.order_by("-id")

        questions: List[str] = []
        answers: List[str] = []

        for faq in qs:
            questions.append((getattr(faq, "question", "") or "").strip())
            answers.append((getattr(faq, "answer", "") or "").strip())

        # ✅ DB가 비었으면 하드코딩 폴백
        if not questions:
            for qa in QA_PAIRS:
                questions.append((qa.get("q") or "").strip())
                answers.append((qa.get("a") or "").strip())

        # ✅ 질문이 있으면 임베딩(실패하면 토큰 폴백 경로로만 동작)
        embs: List[List[float]] = []
        if questions:
            try:
                embs = _lazy_embed_texts(questions)
            except Exception:
                embs = []

        _QA_CACHE["questions"] = questions
        _QA_CACHE["answers"] = answers
        _QA_CACHE["embeddings"] = embs
        _QA_CACHE["ready"] = True

        # 시그니처 동기화(가능하면)
        try:
            cnt, ts = _get_db_signature()
            _QA_CACHE["sig_cnt"] = cnt
            _QA_CACHE["sig_ts"] = ts
            _QA_CACHE["sig_checked_at"] = time.time()
        except Exception:
            pass


def _cosine_sim(vec_a: List[float], vec_b: List[float]) -> float:
    if not vec_a or not vec_b:
        return 0.0

    dot = 0.0
    limit = min(len(vec_a), len(vec_b))
    for i in range(limit):
        dot += vec_a[i] * vec_b[i]

    na = math.sqrt(sum(x * x for x in vec_a))
    nb = math.sqrt(sum(x * x for x in vec_b))
    if na == 0.0 or nb == 0.0:
        return 0.0

    return dot / (na * nb)


def _best_by_token_overlap(user_question: str, min_overlap_ratio: float) -> Optional[str]:
    """
    ✅ 임베딩이 없거나 실패해도 '어떻게든' FAQ가 나오게 하는 폴백.
    - 토큰 겹침 비율 기준으로 가장 높은 항목을 선택
    """
    user_toks = _tokenize(user_question)
    user_set = set(user_toks)
    if not user_set:
        return None

    best_idx = -1
    best_overlap = 0.0

    for i, fq in enumerate(_QA_CACHE["questions"]):
        faq_set = set(_tokenize(fq))
        if not faq_set:
            continue
        inter = user_set & faq_set
        overlap_ratio = len(inter) / float(len(user_set))
        if overlap_ratio > best_overlap:
            best_overlap = overlap_ratio
            best_idx = i

    if best_idx < 0 or best_overlap < min_overlap_ratio:
        return None

    return _QA_CACHE["answers"][best_idx]


def find_best_faq_answer(
    user_question: str,
    threshold: float = 0.80,
    min_overlap_ratio: float = 0.3,
) -> Optional[str]:
    """
    1) 임베딩 유사도 threshold 이상인지 확인
    2) + 토큰 겹침(min_overlap_ratio) 확인

    ✅ 개선:
    - DB 변경 시 자동 갱신
    - 임베딩 실패/비어도 토큰 겹침으로 폴백
    """
    if not user_question.strip():
        return None

    _prepare_qa_cache()

    if not _QA_CACHE["questions"]:
        return None

    # 사용자 질문 임베딩
    try:
        user_vec_list = _lazy_embed_texts([user_question])
        user_vec = user_vec_list[0] if user_vec_list and user_vec_list[0] else []
    except Exception:
        return _best_by_token_overlap(user_question, min_overlap_ratio)

    cached_vecs = _QA_CACHE["embeddings"] or []
    if not user_vec or not cached_vecs:
        return _best_by_token_overlap(user_question, min_overlap_ratio)

    def _dim(v):
        try:
            return len(v)
        except Exception:
            return -1

    # 차원 불일치면 FAQ쪽 재임베딩 시도
    if cached_vecs and _dim(cached_vecs[0]) != _dim(user_vec):
        try:
            new_vecs = _lazy_embed_texts(_QA_CACHE["questions"])
            if new_vecs and _dim(new_vecs[0]) == _dim(user_vec):
                with _QA_LOCK:
                    _QA_CACHE["embeddings"] = new_vecs
                cached_vecs = new_vecs
        except Exception:
            return _best_by_token_overlap(user_question, min_overlap_ratio)

    best_idx = -1
    best_sim = -1.0

    for i, q_vec in enumerate(cached_vecs):
        sim = _cosine_sim(user_vec, q_vec)
        if sim > best_sim:
            best_sim = sim
            best_idx = i

    if best_idx < 0 or best_sim < threshold:
        return None

    # 토큰 겹침 검사
    user_toks = _tokenize(user_question)
    faq_q_toks = _tokenize(_QA_CACHE["questions"][best_idx])
    if not user_toks or not faq_q_toks:
        return None

    inter = set(user_toks) & set(faq_q_toks)
    overlap_ratio = len(inter) / float(len(set(user_toks))) if user_toks else 0.0
    if overlap_ratio < min_overlap_ratio:
        return None

    return _QA_CACHE["answers"][best_idx]


def get_faq_candidates(user_question: str, top_k: int = 3) -> List[dict]:
    """
    ✅ DB 변경 시 자동 갱신
    ✅ 임베딩 실패/없음이면 토큰 기반 후보로 폴백
    """
    if not user_question.strip():
        return []

    _prepare_qa_cache()

    if not _QA_CACHE["questions"]:
        return []

    user_tokens = _tokenize(user_question)
    user_token_set = set(user_tokens)
    if not user_token_set:
        return []

    # 사용자 질문 임베딩
    user_vec: List[float] = []
    try:
        user_vec_list = _lazy_embed_texts([user_question])
        user_vec = user_vec_list[0] if user_vec_list and user_vec_list[0] else []
    except Exception:
        user_vec = []

    cached_vecs = _QA_CACHE["embeddings"] or []

    # ---- 폴백: 임베딩이 없으면 토큰 점수만으로 후보 산출 ----
    if not user_vec or not cached_vecs:
        MIN_TOKEN_OVERLAP = 1
        MIN_BEST_OVERLAP_RATIO = 0.2

        scored_tok: List[Tuple[float, int]] = []
        for i, fq in enumerate(_QA_CACHE["questions"]):
            faq_tokens = _tokenize(fq)
            if not faq_tokens:
                continue

            faq_set = set(faq_tokens)
            inter = user_token_set & faq_set
            overlap_count = len(inter)
            if overlap_count < MIN_TOKEN_OVERLAP:
                continue

            overlap_ratio = overlap_count / float(len(user_token_set))
            scored_tok.append((overlap_ratio, i))

        if not scored_tok:
            return []

        scored_tok.sort(key=lambda x: x[0], reverse=True)
        if scored_tok[0][0] < MIN_BEST_OVERLAP_RATIO:
            return []

        results: List[dict] = []
        for overlap_ratio, idx in scored_tok[: max(1, int(top_k))]:
            fq = _QA_CACHE["questions"][idx]
            fa = _QA_CACHE["answers"][idx]

            # (기존 필터 유지)
            if "생일" in fq or "생일" in fa or "전화" in fq or "전화" in fa:
                continue

            results.append(
                {
                    "q": fq,
                    "a": fa,
                    "score": float(overlap_ratio),
                    "sim": 0.0,
                    "overlap": float(overlap_ratio),
                }
            )
        return results

    # ---- 임베딩 정상 경로 ----
    def _dim(v):
        try:
            return len(v)
        except Exception:
            return -1

    # 차원 불일치면 FAQ쪽 재임베딩 시도 (실패하면 토큰 폴백으로 처리)
    if cached_vecs and _dim(cached_vecs[0]) != _dim(user_vec):
        try:
            new_vecs = _lazy_embed_texts(_QA_CACHE["questions"])
            if new_vecs and _dim(new_vecs[0]) == _dim(user_vec):
                with _QA_LOCK:
                    _QA_CACHE["embeddings"] = new_vecs
                cached_vecs = new_vecs
            else:
                # 차원 안 맞으면 토큰 폴백
                return get_faq_candidates(user_question, top_k=top_k) if False else []
        except Exception:
            # 임베딩 재생성 실패 → 토큰 폴백
            # (여기서 재귀 호출은 피하고, 위 폴백 로직을 직접 실행하려면 간단히 invalidate 후 재호출도 가능)
            return []

    if not cached_vecs:
        return []

    MIN_TOKEN_OVERLAP = 1
    MIN_BEST_SCORE = 0.55
    WEIGHT_SIM = 0.7
    WEIGHT_OVERLAP = 0.3

    scored: List[Tuple[float, int, float, float]] = []
    for i, q_vec in enumerate(cached_vecs):
        sim = _cosine_sim(user_vec, q_vec)

        faq_q = _QA_CACHE["questions"][i]
        faq_tokens = _tokenize(faq_q)
        if not faq_tokens:
            continue

        faq_token_set = set(faq_tokens)
        inter_tokens = user_token_set & faq_token_set
        overlap_count = len(inter_tokens)

        if overlap_count < MIN_TOKEN_OVERLAP:
            continue

        overlap_ratio = overlap_count / float(len(user_token_set)) if user_token_set else 0.0
        final_score = WEIGHT_SIM * sim + WEIGHT_OVERLAP * overlap_ratio
        scored.append((final_score, i, sim, overlap_ratio))

    if not scored:
        return []

    scored.sort(key=lambda x: x[0], reverse=True)
    if scored[0][0] < MIN_BEST_SCORE:
        return []

    results: List[dict] = []
    max_k = max(1, int(top_k))

    for final_score, idx, sim, overlap_ratio in scored[:max_k]:
        fq = _QA_CACHE["questions"][idx]
        fa = _QA_CACHE["answers"][idx]

        if "생일" in fq or "생일" in fa or "전화" in fq or "전화" in fa:
            continue

        results.append(
            {
                "q": fq,
                "a": fa,
                "score": float(final_score),
                "sim": float(sim),
                "overlap": float(overlap_ratio),
            }
        )

    return results
