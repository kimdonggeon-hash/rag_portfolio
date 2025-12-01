# ragapp/qa_data.py

from __future__ import annotations
from typing import List, Dict, Optional
import threading
import math
import re  # (안 써도 괜찮음. 네 원본에 있었으니까 그냥 둠)

# 🔽 추가: DB에서 FAQ 불러오기 위해 import
from django.db.models import QuerySet
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
    # 특수문자 제거 비슷하게
    clean = ""
    for ch in text.lower():
        if ch.isalnum() or ch.isspace():
            clean += ch
        else:
            clean += " "
    # 중복 공백 제거 후 split
    return [tok for tok in clean.split() if tok]


# -----------------------------------------
# 1) 우리가 관리하는 Q/A 쌍들
#    (이건 이제 실제로는 안 쓰이고,
#     대신 DB(FaqEntry)에서 불러온다.
#     남겨두긴 함: 최소 변경을 위해)
# -----------------------------------------
QA_PAIRS: List[Dict[str, str]] = [
    {
        "q": "여기서 무엇을 할 수 있지?",
        "a": "검색 서비스를 기반으로 움직이면 돼.",
    },
    {
        "q": "이걸 왜 만든거야?",
        "a": "재밌잖아?.",
    },
    {
        "q": "이 서비스는 뭐 하는 거예요?",
        "a": "저의 창작물을 마음껏 펼치는 서비스 입니다.",
    },
    {
        "q": "이거는 무엇을 하는거야?",
        "a": "검색놀이",
    },
]


# -----------------------------------------
# 2) 캐시 구조
# -----------------------------------------
_QA_CACHE = {
    "ready": False,
    "questions": [],    # type: List[str]
    "answers": [],      # type: List[str]
    "embeddings": [],   # type: List[List[float]]
}
_QA_LOCK = threading.Lock()


def _lazy_embed_texts(text_list: List[str]) -> List[List[float]]:
    """
    순환 import 방지용 지연 임포트.
    news_services._embed_texts 를 여기서 '나중에' import한다.

    ✅ 변경: 정식 경로(ragapp.services.news_services)를 우선 시도,
             구(舊) 경로(ragapp.news_views.news_services)는 폴백으로 유지.
    """
    try:
        # 최신/정식 위치
        from ragapp.services.news_services import _embed_texts as _real_embed_texts
    except Exception:
        # 예전 배치 호환
        from ragapp.news_views.news_services import _embed_texts as _real_embed_texts
    return _real_embed_texts(text_list)


def _prepare_qa_cache():
    """
    🔄 변경됨:
    예전엔 QA_PAIRS 하드코딩 리스트에서 질문/답변을 읽었는데,
    이제는 DB FaqEntry(is_active=True)에서 가져와서 캐시에 넣는다.

    서버 부팅 이후 첫 호출 때만 로딩해서 _QA_CACHE에 올리고
    _QA_CACHE["ready"] = True 로 플래그 세움.
    (운영 중 FAQ를 바꾸면 서버 재시작 or 이 플래그를 수동으로 False로 만드는 방법으로 갱신 가능)
    """
    with _QA_LOCK:
        if _QA_CACHE["ready"]:
            return

        # DB에서 활성 FAQ만 뽑는다
        qs = (
            FaqEntry.objects
            .filter(is_active=True)
            .order_by("-updated_at", "-created_at")
        )

        questions: List[str] = []
        answers: List[str] = []
        for faq in qs:
            questions.append(faq.question or "")
            answers.append(faq.answer or "")

        # 질문들이 없을 수도 있으니 방어
        if questions:
            try:
                embs = _lazy_embed_texts(questions)  # List[List[float]]
            except Exception:
                embs = []
        else:
            embs = []

        # 캐시에 저장
        _QA_CACHE["questions"]  = questions
        _QA_CACHE["answers"]    = answers
        _QA_CACHE["embeddings"] = embs
        _QA_CACHE["ready"]      = True


def _cosine_sim(vec_a: List[float], vec_b: List[float]) -> float:
    """
    코사인 유사도 (a·b) / (|a||b|)
    """
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


def find_best_faq_answer(
    user_question: str,
    threshold: float = 0.80,
    min_overlap_ratio: float = 0.3,
) -> Optional[str]:
    """
    1) 임베딩 유사도가 threshold 이상인지 확인
    2) + 질문 단어가 실제로도 어느 정도 겹치는지 확인(min_overlap_ratio)

    min_overlap_ratio:
      - 사용자 질문 토큰 중에서 FAQ 질문 토큰과 겹치는 비율
      - 예: 사용자 토큰 5개 중 2개가 FAQ에도 있으면 2/5 = 0.4
      - 이 비율이 너무 낮으면(거의 안 겹치면) FAQ로 안 친다.
    """
    if not user_question.strip():
        return None

    _prepare_qa_cache()

    # 캐시에 FAQ가 1개도 없을 수 있음
    if not _QA_CACHE["questions"]:
        return None

    # 1) 유저 질문 임베딩 (예외 방지)
    try:
        user_vec_list = _lazy_embed_texts([user_question])
    except Exception:
        return None
    if not user_vec_list or not user_vec_list[0]:
        return None
    user_vec = user_vec_list[0]

    # 1.5) 캐시 벡터 차원 확인 → 다르면 재임베딩 시도(가능할 때만)
    def _dim(v):
        try:
            return len(v)
        except Exception:
            return -1

    cached_vecs = _QA_CACHE["embeddings"] or []
    need_reembed = (not cached_vecs) or (_dim(cached_vecs[0]) != _dim(user_vec))
    if need_reembed and _QA_CACHE["questions"]:
        try:
            new_vecs = _lazy_embed_texts(_QA_CACHE["questions"])
            # 차원 맞으면 캐시 갱신
            if new_vecs and _dim(new_vecs[0]) == _dim(user_vec):
                with _QA_LOCK:
                    _QA_CACHE["embeddings"] = new_vecs
                cached_vecs = new_vecs
        except Exception:
            # 재임베딩 실패 시 기존 값으로 진행(유사도는 0으로 나올 수 있음)
            pass

    best_idx = -1
    best_sim = -1.0

    # 2) 가장 비슷한 FAQ 후보 찾기 (임베딩 기준)
    for i, q_vec in enumerate(cached_vecs):
        sim = _cosine_sim(user_vec, q_vec)
        if sim > best_sim:
            best_sim = sim
            best_idx = i

    # 3) 임계치보다 낮으면 그냥 FAQ 포기 -> RAG로 넘김
    if best_idx < 0 or best_sim < threshold:
        return None

    # 4) 추가 안전장치: 실제 단어 겹치는지 검사
    user_toks = _tokenize(user_question)
    faq_q_toks = _tokenize(_QA_CACHE["questions"][best_idx])

    if not user_toks or not faq_q_toks:
        return None

    inter = set(user_toks) & set(faq_q_toks)
    overlap_ratio = (len(inter) / len(set(user_toks))) if user_toks else 0.0

    # 단어가 거의 안 겹치면 "우연히 임베딩이 비슷한 것"일 가능성이 큼 -> FAQ로 안 본다
    if overlap_ratio < min_overlap_ratio:
        return None

    # 여기까지 통과하면 진짜 FAQ로 본다
    return _QA_CACHE["answers"][best_idx]


def get_faq_candidates(user_question: str, top_k: int = 3) -> List[dict]:
    """
    FAQ 확정(threshold 통과)까지는 아니어도,
    RAG 컨텍스트로 줄만한 '유력 FAQ 후보'들을 점수 순으로 top_k개 뽑아준다.

    return 예:
    [
        {
            "q": "운영자의 생일은?",
            "a": "운영자님의 생일은 1996년 11월 6일 입니다.",
            "score": 0.93,   # 최종 점수 (임베딩+토큰겹침)
            "sim": 0.91,     # 코사인 유사도
            "overlap": 0.5,  # 토큰 겹침 비율
        },
        ...
    ]

    ✅ 변경 포인트
    - 사용자 질문과 FAQ 질문이 '토큰이 1개도 안 겹치면' 후보에서 제외.
    - 최종 점수(best_score)가 너무 낮으면(아래 MIN_BEST_SCORE)
      "FAQ 후보 없음"으로 보고 빈 리스트 반환.
    """

    if not user_question.strip():
        return []

    _prepare_qa_cache()

    # 캐시에 FAQ가 없으면 빈 리스트
    if not _QA_CACHE["questions"]:
        return []

    # 0) 사용자 토큰
    user_tokens = _tokenize(user_question)
    user_token_set = set(user_tokens)
    if not user_token_set:
        return []

    # 1) 사용자 질문 임베딩 (예외 방지)
    try:
        user_vec_list = _lazy_embed_texts([user_question])
    except Exception:
        return []
    if not user_vec_list or not user_vec_list[0]:
        return []
    user_vec = user_vec_list[0]

    # 1.5) 차원 정합성 확인 → 필요 시 캐시 재임베딩
    def _dim(v):
        try:
            return len(v)
        except Exception:
            return -1

    cached_vecs = _QA_CACHE["embeddings"] or []
    need_reembed = (not cached_vecs) or (_dim(cached_vecs[0]) != _dim(user_vec))
    if need_reembed and _QA_CACHE["questions"]:
        try:
            new_vecs = _lazy_embed_texts(_QA_CACHE["questions"])
            if new_vecs and _dim(new_vecs[0]) == _dim(user_vec):
                with _QA_LOCK:
                    _QA_CACHE["embeddings"] = new_vecs
                cached_vecs = new_vecs
        except Exception:
            pass

    if not cached_vecs:
        return []

    # 2) 유사도 + 토큰 겹침 기반 점수 계산
    MIN_TOKEN_OVERLAP = 1          # 공통 토큰이 1개 이상 있어야 함
    MIN_BEST_SCORE = 0.55          # 최종 점수(0~1) 이 기준보다 낮으면 FAQ 후보 없음으로 처리
    WEIGHT_SIM = 0.7               # 임베딩 유사도 가중치
    WEIGHT_OVERLAP = 0.3           # 토큰 겹침 비율 가중치

    scored: List[tuple[float, int, float, float]] = []
    for i, q_vec in enumerate(cached_vecs):
        sim = _cosine_sim(user_vec, q_vec)

        faq_q = _QA_CACHE["questions"][i]
        faq_tokens = _tokenize(faq_q)
        if not faq_tokens:
            continue

        faq_token_set = set(faq_tokens)
        inter_tokens = user_token_set & faq_token_set
        overlap_count = len(inter_tokens)

        # 👉 공통 토큰이 하나도 없으면, 의미상 완전히 다른 질문이므로 스킵
        if overlap_count < MIN_TOKEN_OVERLAP:
            continue

        overlap_ratio = overlap_count / float(len(user_token_set)) if user_token_set else 0.0

        # 최종 점수 = 임베딩 유사도와 토큰 겹침 비율을 섞어서 계산
        final_score = WEIGHT_SIM * sim + WEIGHT_OVERLAP * overlap_ratio

        scored.append((final_score, i, sim, overlap_ratio))

    if not scored:
        # 어떤 FAQ도 질문과 공통 토큰이 없거나 점수가 너무 낮은 경우
        return []

    # 점수 높은 순으로 정렬
    scored.sort(key=lambda x: x[0], reverse=True)

    best_final_score = scored[0][0]
    if best_final_score < MIN_BEST_SCORE:
        # 전체적으로 질문과 FAQ가 너무 안 맞으면 아예 FAQ 후보를 쓰지 않는다.
        return []

    results: List[dict] = []
    max_k = max(1, int(top_k))

    for final_score, idx, sim, overlap_ratio in scored[:max_k]:
        fq = _QA_CACHE["questions"][idx]
        fa = _QA_CACHE["answers"][idx]

        # 🔒 민감한 답변이면 여기서 제외 (예: 생일/전화 등)
        if "생일" in fq or "생일" in fa or "전화" in fq or "전화" in fa:
            continue

        results.append(
            {
                "q": fq,
                "a": fa,
                "score": float(final_score),     # 최종 점수
                "sim": float(sim),               # 코사인 유사도
                "overlap": float(overlap_ratio), # 토큰 겹침 비율
            }
        )

    return results
