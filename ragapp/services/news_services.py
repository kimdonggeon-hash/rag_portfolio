# ragapp/services/news_services.py
from __future__ import annotations

import os
import re
import json
import hashlib
import logging
from typing import List, Dict, Tuple, Optional, Any
from datetime import datetime
from urllib.parse import quote_plus, urlparse
from pathlib import Path
from functools import lru_cache

from ragapp.services.vdb_store import (
    vdb_query_vector_only as _hy_vec_query,
    vdb_upsert_docs as _hy_upsert,
    vdb_query_hybrid as _hy_query,
    vdb_db_path as _hy_db_path,
)

from django.conf import settings

try:
    # DB 기반 FAQ 후보 가져오는 함수
    from ragapp.qa_data import get_faq_candidates  # type: ignore
except Exception:
    get_faq_candidates = None  # type: ignore

log = logging.getLogger(__name__)

# =============================================================================
# ✅ MAX LEGAL-SAFE POLICY (고정: 외부 기사 "본문"은 절대 수집/저장/인덱싱하지 않음)
# =============================================================================
NEWS_SAFE_MODE: bool = True  # 🔒 하드락(환경변수로 해제 불가)
NEWS_STORE_FULLTEXT: bool = False  # 🔒 하드락
NEWS_SNIPPET_MAX_CHARS: int = int(os.environ.get("NEWS_SNIPPET_MAX_CHARS", "240"))
NEWS_PREVIEW_MAX_CHARS: int = int(os.environ.get("NEWS_PREVIEW_MAX_CHARS", "300"))

# =============================================================================
# RAG/프롬프트 예산(최적화)
# =============================================================================
RAG_SOURCE_BLOCK_MAX_CHARS: int = int(
    getattr(settings, "RAG_SOURCE_BLOCK_MAX_CHARS", os.environ.get("RAG_SOURCE_BLOCK_MAX_CHARS", "4500"))
)
RAG_SOURCE_SNIPPET_MAX_CHARS: int = int(
    getattr(settings, "RAG_SOURCE_SNIPPET_MAX_CHARS", os.environ.get("RAG_SOURCE_SNIPPET_MAX_CHARS", "700"))
)
RAG_SOURCE_SNIPPET_MIN_CHARS: int = int(
    getattr(settings, "RAG_SOURCE_SNIPPET_MIN_CHARS", os.environ.get("RAG_SOURCE_SNIPPET_MIN_CHARS", "120"))
)
RAG_HISTORY_MAX_CHARS: int = int(
    getattr(settings, "RAG_HISTORY_MAX_CHARS", os.environ.get("RAG_HISTORY_MAX_CHARS", "900"))
)
RAG_USE_HISTORY_IN_PROMPT: bool = bool(
    str(getattr(settings, "RAG_USE_HISTORY_IN_PROMPT", os.environ.get("RAG_USE_HISTORY_IN_PROMPT", "0"))).lower()
    in ("1", "true", "yes", "y", "on")
)

# =============================================================================
# 0) Google GenAI (Vertex 백엔드 고정): 텍스트 생성 / 임베딩
# =============================================================================

_GENAI_CLIENT = None


def _check_adc_env(project: Optional[str], location: Optional[str]) -> None:
    """ADC(서비스 계정) 환경 사전 점검: 프로젝트/리전/GAC 파일 경로 확인."""
    gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or getattr(
        settings, "GOOGLE_APPLICATION_CREDENTIALS", ""
    )
    gac = str(gac).strip()
    if not project:
        log.warning("Vertex 프로젝트 ID가 비어있습니다. (.env의 VERTEX_PROJECT_ID 또는 GOOGLE_CLOUD_PROJECT)")
    if not location:
        log.warning("Vertex 리전이 비어있습니다. (.env의 VERTEX_LOCATION 또는 GCP_LOCATION)")
    if gac:
        if not Path(gac).exists():
            log.warning("GOOGLE_APPLICATION_CREDENTIALS 경로가 존재하지 않습니다: %s", gac)
    else:
        log.debug("GOOGLE_APPLICATION_CREDENTIALS 미설정: gcloud/OS 로그인 등 다른 ADC 경로를 사용합니다.")


def _genai_client():
    """Vertex AI 백엔드로 고정된 google-genai Client 생성(1회 캐시)."""
    global _GENAI_CLIENT
    if _GENAI_CLIENT is not None:
        return _GENAI_CLIENT

    try:
        from google import genai
        from google.genai import types as genai_types  # 일부 배포판 호환
    except Exception as e:
        raise RuntimeError(
            "google-genai 패키지가 없습니다. `pip install -U google-genai` 후 다시 시도하세요."
        ) from e

    project = getattr(settings, "VERTEX_PROJECT_ID", None) or os.environ.get("VERTEX_PROJECT_ID")

    # ✅ Gemini 생성 모델 전용 location
    # - VERTEX_LOCATION은 도쿄 유지
    # - gemini-3.5-flash 호출은 VERTEX_LLM_LOCATION=global 사용
    location = (
        getattr(settings, "VERTEX_LLM_LOCATION", None)
        or os.environ.get("VERTEX_LLM_LOCATION")
        or "global"
    )

    _check_adc_env(project, location)

    if not project:
        raise RuntimeError("VERTEX_PROJECT_ID 가 설정되어야 합니다. (settings.py 또는 환경변수)")

    # Vertex 라우팅 + stable v1 (가능한 한 보수적으로)
    try:
        http_opts = genai_types.HttpOptions(api_version="v1")
    except Exception:
        from google import genai as _g
        http_opts = _g.types.HttpOptions(api_version="v1")

    _GENAI_CLIENT = genai.Client(
        vertexai=True,
        project=project,
        location=location,
        http_options=http_opts,
    )
    return _GENAI_CLIENT


def _first_non_empty(values: list[str | None]) -> str | None:
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _choose_text_model(_explicit_ignored: Optional[str]) -> str:
    """
    텍스트 모델은 무조건 .env에서만 선택.
    우선순위: GEMINI_MODEL_DIRECT > VERTEX_TEXT_MODEL > GEMINI_TEXT_MODEL > GEMINI_MODEL > GEMINI_MODEL_DEFAULT
    """
    picked = _first_non_empty(
        [
            os.environ.get("GEMINI_MODEL_DIRECT"),
            os.environ.get("VERTEX_TEXT_MODEL"),
            os.environ.get("GEMINI_TEXT_MODEL"),
            os.environ.get("GEMINI_MODEL"),
            os.environ.get("GEMINI_MODEL_DEFAULT"),
        ]
    )
    if picked:
        return picked
    raise RuntimeError(
        "텍스트 생성 모델명이 .env에 없습니다. "
        "GEMINI_MODEL_DIRECT 또는 VERTEX_TEXT_MODEL/GEMINI_TEXT_MODEL/GEMINI_MODEL 중 하나를 설정하세요."
    )


def _env_embed_model() -> str:
    """임베딩 모델은 무조건 .env에서만 선택."""
    v = os.environ.get("VERTEX_EMBED_MODEL")
    if v and v.strip():
        return v.strip()
    m = os.environ.get("GEMINI_EMBED_MODELS")
    if m and m.strip():
        return m.split(",")[0].strip()
    raise RuntimeError(
        "임베딩 모델명이 .env에 없습니다. VERTEX_EMBED_MODEL 또는 GEMINI_EMBED_MODELS 를 설정하세요."
    )


_EMPTY_FALLBACK = "API가 답변을 반환하지 않았습니다."


def _extract_text_from_genai_response(resp) -> str:
    """google-genai 응답에서 텍스트를 최대한 호환성 있게 추출. (예외 던지지 않음)"""
    try:
        t = getattr(resp, "text", None) or getattr(resp, "output_text", None)
        if isinstance(t, str) and t.strip():
            return t.strip()
    except Exception:
        pass

    try:
        cands = getattr(resp, "candidates", None) or []
        for c in cands:
            content = getattr(c, "content", None)
            if not content:
                continue
            parts = getattr(content, "parts", None) or []
            texts = []
            for p in parts:
                txt = getattr(p, "text", None)
                if isinstance(txt, str) and txt.strip():
                    texts.append(txt.strip())
                elif isinstance(p, str) and p.strip():
                    texts.append(p.strip())
            if texts:
                return "\n".join(texts).strip()
    except Exception:
        pass

    try:
        if isinstance(resp, dict):
            if isinstance(resp.get("text"), str) and resp["text"].strip():
                return resp["text"].strip()
            for c in resp.get("candidates") or []:
                parts = (((c or {}).get("content") or {}).get("parts") or [])
                texts = []
                for p in parts:
                    if isinstance(p, dict) and isinstance(p.get("text"), str) and p["text"].strip():
                        texts.append(p["text"].strip())
                    elif isinstance(p, str) and p.strip():
                        texts.append(p.strip())
                if texts:
                    return "\n".join(texts).strip()
    except Exception:
        pass

    return ""


def ask_gemini(prompt: str, model: Optional[str] = None) -> str:
    """
    Vertex 경유 텍스트 생성 (google-genai).
    - 절대 빈 문자열을 반환하지 않음
    """
    client = _genai_client()
    mdl_name = _choose_text_model(model)

    try:
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        resp = client.models.generate_content(model=mdl_name, contents=contents)
        txt = _extract_text_from_genai_response(resp)
        if txt and txt.strip():
            return txt.strip()

        # SDK 호환(일부 버전은 contents=str 허용)
        resp2 = client.models.generate_content(model=mdl_name, contents=prompt)
        txt2 = _extract_text_from_genai_response(resp2)
        if txt2 and txt2.strip():
            return txt2.strip()

        log.warning("ask_gemini: empty text (model=%s)", mdl_name)
        return "모델이 텍스트 본문을 반환하지 않았습니다. 로그를 확인해 주세요."

    except Exception as e:
        log.warning("ask_gemini 실패(model=%s): %s", mdl_name, e)
        return f"모델 호출 실패: {e}"


# -------------------------
# Vertex 임베딩 최적화(캐시)
# -------------------------

@lru_cache(maxsize=1)
def _vertex_project_location() -> Tuple[str, str]:
    project = (
        getattr(settings, "VERTEX_PROJECT_ID", None)
        or os.environ.get("VERTEX_PROJECT_ID")
        or getattr(settings, "VERTEX_PROJECT", None)
        or os.environ.get("VERTEX_PROJECT")
    )

    # ✅ 임베딩 전용 location
    # - 없으면 기존 VERTEX_LOCATION으로 fallback
    location = (
        getattr(settings, "VERTEX_EMBED_LOCATION", None)
        or os.environ.get("VERTEX_EMBED_LOCATION")
        or os.environ.get("EMBED_LOCATION")
        or getattr(settings, "VERTEX_LOCATION", None)
        or os.environ.get("VERTEX_LOCATION")
        or "asia-northeast3"
    )

    if not project:
        raise RuntimeError("VERTEX_PROJECT_ID/PROJECT 미설정")
    return str(project), str(location)


@lru_cache(maxsize=1)
def _vertex_embedding_model():
    """
    TextEmbeddingModel 객체를 1회만 생성해서 재사용.
    - 호출마다 from_pretrained/vertexai.init 반복하지 않도록 최적화
    """
    project, location = _vertex_project_location()

    import vertexai
    try:
        vertexai.init(project=project, location=location)
    except Exception:
        pass

    try:
        from vertexai.preview.language_models import TextEmbeddingModel  # type: ignore
    except Exception:
        from vertexai.language_models import TextEmbeddingModel  # type: ignore

    model_name = _env_embed_model()
    return TextEmbeddingModel.from_pretrained(model_name)


def _embed_texts(texts: List[str]) -> List[List[float]]:
    """
    텍스트 리스트 → 임베딩 리스트.
    - Vertex SDK(TextEmbeddingModel)만 사용
    - 최적화: vertex init + model 로딩 1회 캐시
    """
    if not texts:
        return []

    batch: List[str] = []
    for t in texts:
        s = ("" if t is None else str(t)).strip()
        batch.append(s if s else " ")

    try:
        model = _vertex_embedding_model()

        try:
            emb_objs = model.get_embeddings(batch)
        except TypeError:
            emb_objs = model.get_embeddings(input=batch)

        def _vec_from(obj) -> List[float]:
            if hasattr(obj, "values"):
                return [float(x) for x in list(getattr(obj, "values"))]
            if hasattr(obj, "embedding"):
                emb = getattr(obj, "embedding")
                if hasattr(emb, "values"):
                    return [float(x) for x in list(getattr(emb, "values"))]
                if isinstance(emb, (list, tuple)):
                    return [float(x) for x in emb]
            if isinstance(obj, dict):
                if "values" in obj and isinstance(obj["values"], (list, tuple)):
                    return [float(x) for x in obj["values"]]
                emb = obj.get("embedding")
                if isinstance(emb, dict) and "values" in emb:
                    return [float(x) for x in emb["values"]]
                if isinstance(emb, (list, tuple)):
                    return [float(x) for x in emb]
            return [float(x) for x in list(obj)]

        if isinstance(emb_objs, list):
            out = [_vec_from(e) for e in emb_objs]
        else:
            cand = getattr(emb_objs, "embeddings", None)
            out = [_vec_from(e) for e in (cand or [emb_objs])]

        if not out or any(not v for v in out):
            raise RuntimeError("Vertex 임베딩 응답 파싱 실패")

        return out

    except Exception as e_vertex:
        log.warning("Vertex 임베딩 실패: %s", e_vertex)
        raise RuntimeError(f"임베딩 실패(Vertex): {e_vertex}") from e_vertex


# =============================================================================
# ❶ 로컬 SQLite 하이브리드 스토어(단일 소스 오브 트루스: sqlite_hybrid_store.py)
# =============================================================================

_VECTOR_DB_PATH = _hy_db_path()


def _chroma_collection():
    return None


# =============================================================================
# 1) 뉴스 RSS (META ONLY)
# =============================================================================

def _strip_html(s: str) -> str:
    if not s:
        return ""
    t = re.sub(r"<[^>]+>", " ", s)
    t = re.sub(r"&nbsp;|&#160;", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _clean_text_for_preview(raw_text: str, fallback_snippet: str = "", max_len: int = 500) -> str:
    """
    화면/메타 저장용 미리보기는 항상 짧게.
    (외부 원문을 길게 저장/노출하는 방향을 피함)
    """
    base = (raw_text or "").strip()
    if not base:
        base = (fallback_snippet or "").strip()

    base = _strip_html(base)
    base = re.sub(r"[ \t]+", " ", base).strip()

    if len(base) < 30 and fallback_snippet:
        base = _strip_html((fallback_snippet or "").strip())

    return base[:max_len]


def fetch_article_text(url: str, timeout: int = 12) -> str:
    """
    🔒 MAX LEGAL-SAFE: 외부 기사 본문을 절대 가져오지 않습니다.
    - 항상 빈 문자열 반환
    """
    _ = (url, timeout)
    return ""


def crawl_news_bodies(news_list: List[Dict[str, str]], max_workers: int = 6) -> List[Dict[str, str]]:
    """
    🔒 MAX LEGAL-SAFE:
    - news_body: 항상 빈 문자열
    - news_preview: RSS snippet만 짧게 정리
    """
    _ = max_workers
    out = [dict(n) for n in (news_list or [])]
    for n in out:
        snippet = (n.get("snippet") or "").strip()
        n["news_body"] = ""
        n["news_preview"] = _clean_text_for_preview(
            "",
            fallback_snippet=snippet[: max(80, NEWS_SNIPPET_MAX_CHARS)],
            max_len=NEWS_PREVIEW_MAX_CHARS,
        )
        n["body_len"] = 0
    return out


def search_news_rss(query: str, top_k: int, *, recency_days: Optional[int] = None) -> List[Dict[str, str]]:
    """
    Google News RSS 검색.
    - recency_days가 있으면 when:Nd 를 query에 붙여 최신 범위를 좁힘(비용/품질 안정화)
    """
    tmpl = getattr(
        settings,
        "NEWS_RSS_QUERY_TEMPLATE",
        os.environ.get(
            "NEWS_RSS_QUERY_TEMPLATE",
            "https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko",
        ),
    )

    q = (query or "").strip()
    if recency_days and isinstance(recency_days, int) and recency_days > 0:
        # ✅ Google News 고급검색 문법: when:7d
        q = f"{q} when:{int(recency_days)}d"

    url = tmpl.format(query=quote_plus(q))

    try:
        import feedparser
    except Exception as e:
        log.warning("feedparser 미설치 또는 오류: %s", e)
        return []

    feed = feedparser.parse(url)
    arts: List[Dict[str, str]] = []
    for entry in feed.get("entries", [])[: max(1, int(top_k))]:
        link = entry.get("link", "") or ""
        src = ""
        try:
            src = (entry.get("source") or {}).get("title", "") or ""
        except Exception:
            src = ""
        if not src:
            try:
                src = urlparse(link).netloc
            except Exception:
                src = ""

        summary_raw = (entry.get("summary", "") or "").strip()
        snippet = _clean_text_for_preview(
            "",
            fallback_snippet=summary_raw,
            max_len=max(80, NEWS_SNIPPET_MAX_CHARS),
        )

        arts.append(
            {
                "title": (entry.get("title", "") or "").strip(),
                "url": link.strip(),
                "source": (src or "").strip(),
                "published_at": (entry.get("published") or entry.get("updated") or "").strip(),
                "snippet": snippet,
            }
        )
    return arts


# ---------------------------------------------------------------------
# Time-sensitivity scoring (확장형)
# - 정규식 '하드코딩 확장' 대신, 약한 힌트를 여러 개 합산해서 판단
# - recency_days를 단계적으로 결정해 비용/품질 균형
# ---------------------------------------------------------------------

_TIME_WORDS = {
    "today": ["오늘", "금일", "오늘자", "당일"],
    "now": ["지금", "현재", "방금", "막"],
    "realtime": ["실시간", "라이브"],
    "recent": ["최신", "최근", "요즘", "근황", "급상승"],
    "period_week": ["이번주", "이번 주", "금주", "지난주", "지난 주", "전주", "다음주", "다음 주"],
    "period_month": ["이번달", "이번 달", "금월", "지난달", "지난 달", "전월", "다음달", "다음 달"],
    "period_year": ["올해", "금년", "작년", "전년", "내년"],
    "update": ["업데이트", "갱신", "변경", "개편", "발표", "공개", "출시", "런칭", "개봉", "방영", "상영"],
}

_VOLATILE_TOPICS = {
    # 변동성이 큰 것들(강하게 최신)
    "market": ["환율", "금리", "주가", "증시", "지수", "코스피", "코스닥", "나스닥", "다우", "s&p", "시세", "가격"],
    "crypto": ["비트코인", "이더리움", "코인", "암호화폐", "가상자산"],
    "weather": ["날씨", "기온", "미세먼지", "초미세먼지", "강수", "비", "눈", "태풍"],
    "sports": ["경기", "결과", "스코어", "득점", "승패", "순위표", "리그"],
    "ticket": ["예매", "예약", "예매율", "대기", "품절", "재고"],
    "boxoffice": ["박스오피스", "box office", "관객수"],
}

_RANK_WORDS = ["1위", "2위", "3위", "순위", "랭킹", "등수", "top", "탑", "베스트", "인기", "차트"]

def _norm_q(q: str) -> str:
    return re.sub(r"\s+", " ", (q or "").strip().lower())

def _contains_any(qn: str, words: list[str]) -> bool:
    return any(w.lower() in qn for w in words if w)

def _time_sensitivity_score(q: str) -> Tuple[int, Dict[str, bool]]:
    """
    점수 + 플래그 반환.
    - has_update: '업데이트/발표/출시/개봉/방영' 같은 이벤트성 키워드 감지용
    - 점수는 '최신 필요' 정도(클수록 최신 민감)
    - 플래그는 recency/ctx 결정을 위한 힌트
    """
    qn = _norm_q(q)
    flags = {
        "has_time": False,
        "has_rank": False,
        "has_volatile": False,
        "has_update": False,
        "has_strong_time": False,  # ✅ 오늘/지금/실시간 등 강한 힌트
    }
    score = 0

    # 시간/기간 힌트
    for k, words in _TIME_WORDS.items():
        if _contains_any(qn, words):
            flags["has_time"] = True

            if k in ("today", "now", "realtime"):
                flags["has_strong_time"] = True
                score += 2
            elif k == "recent":
                # ✅ "최근/요즘/최신"은 검색 유도 힌트지만 강제 최신 트리거로 쓰면 과해짐 → 약하게
                score += 1
            else:
                score += 1

            if k == "update":
                flags["has_update"] = True

    # 랭킹/순위 힌트
    if _contains_any(qn, _RANK_WORDS) or re.search(r"\btop\s*\d+\b", qn):
        flags["has_rank"] = True
        score += 2

    # 변동성 큰 토픽(강한 최신)
    for _, words in _VOLATILE_TOPICS.items():
        if _contains_any(qn, words):
            flags["has_volatile"] = True
            score += 3
            break

    # 너무 짧은 질문(예: "1위 뭐야")은 최신일 가능성↑
    if len(qn) <= 10 and flags["has_rank"]:
        score += 1

    return score, flags

def _decide_recency_days(q: str) -> int:
    """
    비용/품질 균형:
    - 변동성 높은 토픽 + 현재/순위: 1~3일
    - 업데이트/발표/출시: 7~21일
    - 그 외 시간 힌트: 14일
    - 기본: 30일
    """
    score, flags = _time_sensitivity_score(q)

    # 강한 최신성
    if flags["has_volatile"] and (flags["has_time"] or flags["has_rank"]):
        return int(getattr(settings, "NEWS_RSS_RECENCY_VOLATILE", os.environ.get("NEWS_RSS_RECENCY_VOLATILE", "3")))

    if flags["has_rank"] and flags["has_time"]:
        return int(getattr(settings, "NEWS_RSS_RECENCY_RANK", os.environ.get("NEWS_RSS_RECENCY_RANK", "7")))

    if flags["has_update"]:
        return int(getattr(settings, "NEWS_RSS_RECENCY_UPDATE", os.environ.get("NEWS_RSS_RECENCY_UPDATE", "21")))

    if flags["has_time"] or flags["has_rank"]:
        return int(getattr(settings, "NEWS_RSS_RECENCY_TIME", os.environ.get("NEWS_RSS_RECENCY_TIME", "14")))

    # 기본
    return int(getattr(settings, "NEWS_RSS_RECENCY_DAYS", os.environ.get("NEWS_RSS_RECENCY_DAYS", "30")))

def _should_attach_time_ctx(q: str) -> bool:
    """
    프롬프트에 '기준 시각(KST)' 헤더를 붙일지 결정.
    ✅ 보수적으로:
      - 변동성 토픽은 항상 True
      - (시간+순위) 조합도 True
      - 강한 시간 힌트(오늘/지금/실시간) 정도는 True
      - 그 외(최근/요즘만)는 False
    """
    score, flags = _time_sensitivity_score(q)

    if flags.get("has_volatile"):
        return True

    if flags.get("has_time") and flags.get("has_rank"):
        return True

    # ✅ 오늘/지금/실시간 같은 강한 힌트가 있을 때만 time_ctx 켜기
    if flags.get("has_strong_time"):
        return True

    # 나머지는 너무 민감하게 켜지지 않도록 OFF
    _ = score
    return False

def _needs_web_sources(q: str) -> bool:
    """
    ✅ '실시간/최신/순위/가격/뉴스' 등 근거가 필요한 질문만 True
    - 정의/설명/개념/원리/사용법 같은 일반 질문은 False(=직답으로 처리)
    """
    qn = _norm_q(q)

    # 강한 트리거(실시간/순위/가격 등)
    if _contains_any(qn, _RANK_WORDS):
        return True
    for _, words in _VOLATILE_TOPICS.items():
        if _contains_any(qn, words):
            return True

    # 시간/최신성 힌트가 강한 경우만
    score, flags = _time_sensitivity_score(q)
    if flags.get("has_strong_time"):
        return True

    # "최근/요즘/최신"만 단독이면 과민 반응 방지 → False
    # 다만 "뉴스"가 같이 있으면 True
    if ("뉴스" in qn) or ("headline" in qn) or ("헤드라인" in qn):
        return True

    return False


def _direct_answer_prompt(q: str, ctx: Dict[str, Any]) -> str:
    """
    ✅ 웹근거 없이도 답할 수 있는 질문용(일상/설명/조언 전부)
    """
    now_kst_iso = (ctx.get("now_kst_iso") or "").strip()
    time_line = f"(참고: KST {now_kst_iso})\n\n" if now_kst_iso else ""

    return (
        time_line
        + "한국어로 자연스럽고 실용적으로 답하세요.\n"
        + "- 사용자가 일상/생활 질문을 하면: 결론 1줄 → 바로 할 일(체크리스트 3~6개) → 주의사항 1~2개\n"
        + "- 정의/개념 질문이면: 1) 한줄 정의 2) 핵심 포인트 3) 짧은 예시\n"
        + "- 최신/실시간 확인이 꼭 필요한 경우에만 '실시간 확인 필요'라고 말하고, 대신 확인 방법/검색어를 제안하세요.\n"
        + "- 모르면 단정하지 말고 '가능성/조건'으로 안내하세요.\n\n"
        + f"[질문]\n{q}\n\n[답변]\n"
    )


def _augment_query_with_time_ctx(q: str, ctx: Dict[str, Any]) -> str:
    """
    ✅ 검색 쿼리 보강은 '하지 않음'이 기본.
    - Google News RSS는 괄호/기준 문구 같은 잡음이 섞이면 오히려 검색 질이 떨어질 수 있음
    - 최신성은 when:Nd(=recency_days)로 해결
    - 기준시각/오늘 날짜는 '프롬프트 헤더'로만 주입
    """
    _ = ctx
    return (q or "").strip()


def gemini_answer_with_news(
    question: str,
    *,
    ctx: Optional[Dict[str, Any]] = None,
) -> Tuple[str, List[Dict[str, str]]]:
    """
    Web 모드(메타-only):
    1) RSS 헤드라인(meta-only) 먼저 수집
    2) 헤드라인을 근거로 Vertex 답변 생성
    3) 링크 목록은 모델이 아니라 서버가 붙임(링크 환각 방지)
    """
    ctx = ctx or {}
    q = (question or "").strip()

    if not _needs_web_sources(q):
        prompt_direct = _direct_answer_prompt(q, ctx)
        answer_direct = ask_gemini(prompt_direct, model=None)
        if not isinstance(answer_direct, str) or not answer_direct.strip():
            answer_direct = _EMPTY_FALLBACK
        return answer_direct.strip(), []

    # 1) 최신성 판단
    recency_days = _decide_recency_days(q)
    use_time_ctx = _should_attach_time_ctx(q)

    # 2) 검색 쿼리 보강(필요할 때만)
    q_for_search = _augment_query_with_time_ctx(q, ctx) if use_time_ctx else q

    # 3) RSS 헤드라인 먼저 수집 (meta-only)
    try:
        topk = int(getattr(settings, "NEWS_TOPK", os.environ.get("NEWS_TOPK", "5")))
    except Exception:
        topk = 5

    if use_time_ctx:
        try:
            topk_ts = int(getattr(settings, "NEWS_TOPK_TS", os.environ.get("NEWS_TOPK_TS", "8")))
        except Exception:
            topk_ts = 8
        topk = max(topk, topk_ts)

    headlines = search_news_rss(q_for_search, topk, recency_days=recency_days) or []
    if not isinstance(headlines, list):
        headlines = []

    # ✅ (추천) usable만 남기기: 제목/URL 중 하나라도 있어야 참고자료로 취급
    usable = []
    for h in headlines:
        if isinstance(h, dict) and ((h.get("title") or "").strip() or (h.get("url") or h.get("link") or "").strip()):
            usable.append(h)
    headlines = usable

    # ✅ 참고자료가 없으면: grounded 프롬프트 금지 → 직답 프롬프트
    if not headlines:
        prompt_direct = _direct_answer_prompt(q, ctx)
        answer_direct = ask_gemini(prompt_direct, model=None)
        if not isinstance(answer_direct, str) or not answer_direct.strip():
            answer_direct = _EMPTY_FALLBACK
        return answer_direct.strip(), []

    # 4) 기준시각(KST) 헤더
    today_iso = (ctx.get("today_iso") or "").strip()
    today_kr = (ctx.get("today_kr") or "").strip()
    dow_kr = (ctx.get("dow_kr") or "").strip()
    now_kst_iso = (ctx.get("now_kst_iso") or "").strip()

    time_header = ""
    if use_time_ctx and (today_iso or today_kr or now_kst_iso):
        time_header = (
            "아래 기준시각은 서버(KST)에서 제공된 값입니다. "
            "시간에 민감한 질문은 이 기준을 따르세요.\n"
            f"- 오늘 날짜: {today_iso or today_kr}{f'({dow_kr})' if dow_kr else ''}\n"
            f"- 기준 시각(KST): {now_kst_iso}\n\n"
        )

    # 5) 헤드라인(meta) 근거 텍스트 만들기
    def _pick(h: dict, *keys: str) -> str:
        for k in keys:
            v = h.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    max_refs = 6  # 프롬프트 길이/비용 캡
    ref_lines: list[str] = []
    for i, h in enumerate(headlines[:max_refs], start=1):
        if not isinstance(h, dict):
            continue
        title = _pick(h, "title") or "(제목 없음)"
        url = _pick(h, "url", "link")
        source = _pick(h, "source", "publisher", "site")
        published = _pick(h, "published_at", "date", "pubDate")
        snippet = _pick(h, "snippet", "summary", "description")
        if snippet:
            snippet = (snippet[:260] + "…") if len(snippet) > 260 else snippet

        ref_lines.append(
            f"[{i}] {title}\n"
            f"- 출처: {source or '-'}\n"
            f"- 게시: {published or '-'}\n"
            f"- URL: {url or '-'}\n"
            f"- 요약: {snippet or '-'}"
        )

    refs_block = "\n\n".join(ref_lines).strip()

    if not refs_block:
        prompt_direct = _direct_answer_prompt(q, ctx)
        answer_direct = ask_gemini(prompt_direct, model=None)
        if not isinstance(answer_direct, str) or not answer_direct.strip():
            answer_direct = _EMPTY_FALLBACK
        return answer_direct.strip(), []

    # 6) 프롬프트: 참고자료 기반 + 링크는 쓰지 말기(서버가 붙임)
    prompt = (
        time_header
        + "한국어로 간결하게 답하세요.\n"
        + "아래 [참고자료]에 있는 내용만 근거로 요약/정리하세요.\n"
        + "참고자료로 단정할 수 없으면 단정하지 말고, '가능/불확실'을 나눠 한 줄로 표시하세요.\n"
        + "기사 본문을 복사하거나 길게 인용하지 마세요.\n"
        + "중요: 링크(URL)는 답변에 직접 적지 마세요. (링크는 시스템이 별도로 붙입니다)\n"
        + "시간에 따라 변하는 값(현재/오늘/최신/순위/시세/환율/주가/날씨/경기결과 등)은 "
          "반드시 '기준 시각(KST)'을 한 줄로 명시하세요.\n\n"
        + f"[질문]\n{q}\n\n"
        + f"[참고자료]\n{refs_block if refs_block else '(참고자료 없음)'}\n\n"
        + "[답변]\n"
    )

    answer_text = ask_gemini(prompt, model=None)
    if not isinstance(answer_text, str) or not answer_text.strip():
        answer_text = _EMPTY_FALLBACK

    # 7) 링크 목록은 서버가 headlines로만 구성 (환각 방지)
    def _link_list(heads: list[dict], n: int = 5) -> str:
        items: list[str] = []
        for h in heads[:n]:
            if not isinstance(h, dict):
                continue
            t = _pick(h, "title") or "(제목 없음)"
            u = _pick(h, "url", "link")
            if not u:
                continue
            items.append(f"- {t} — {u}")
        return "\n".join(items)

    links = _link_list(headlines, n=5)
    if links:
        answer_text = (answer_text.rstrip() + "\n\n[참고 링크]\n" + links).strip()

    # (선택) 기준시각 보강(모델 누락 대비)
    if use_time_ctx and now_kst_iso:
        low = answer_text.lower()
        if ("기준 시각" not in answer_text) and ("kst" not in low):
            answer_text = (answer_text.rstrip() + f"\n\n(기준: KST {now_kst_iso})").strip()

    return answer_text.strip(), headlines

# =============================================================================
# 2) 인덱싱 (MAX LEGAL-SAFE: 뉴스는 meta-only만)
# =============================================================================

def _sha(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8", "ignore")).hexdigest()[:16]


def _slug(s: str, n: int = 60) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z가-힣\-_. ]+", "", s or "")
    cleaned = re.sub(r"\s+", "-", cleaned).strip("-")
    return cleaned[:n] or "doc"


def _iso(dt) -> str:
    from email.utils import parsedate_to_datetime

    try:
        if isinstance(dt, datetime):
            return dt.isoformat()
        if not dt:
            return ""
        try:
            return parsedate_to_datetime(dt).isoformat()
        except Exception:
            return datetime.fromisoformat(str(dt).replace("Z", "+00:00")).isoformat()
    except Exception:
        return ""


def _chunk_text(text: str, size=1600, overlap=200) -> List[str]:
    t = (text or "").strip()
    if not t:
        return []
    out = []
    i = 0
    n = len(t)
    while i < n:
        j = min(i + size, n)
        out.append(t[i:j])
        if j == n:
            break
        i = j - overlap
    return out


def chroma_upsert(*args, **kwargs) -> Dict[str, Any]:
    """
    Compatibility upsert.
    임베딩이 비어오면 자동으로 생성합니다.
    """
    if args and len(args) == 4:
        ids_a, docs_a, metas_a, embs_a = args
        ids = list(ids_a or [])
        documents = list(docs_a or [])
        metadatas = list(metas_a or [{} for _ in documents])
        embeddings = list(embs_a or [])
    else:
        ids = list(kwargs.get("ids") or [])
        documents = list(kwargs.get("documents") or kwargs.get("docs") or [])
        metadatas = list(kwargs.get("metadatas") or kwargs.get("metas") or [{} for _ in documents])
        embeddings = list(kwargs.get("embeddings") or kwargs.get("embs") or [])

    n = len(documents)
    if len(metadatas) != n:
        if len(metadatas) < n:
            metadatas.extend({} for _ in range(n - len(metadatas)))
        else:
            metadatas = metadatas[:n]

    if not embeddings or len(embeddings) != n:
        embeddings = _embed_texts(documents)

    if not ids or len(ids) != n:
        now_iso = datetime.utcnow().isoformat()
        ids = [f"doc:{_sha((documents[i] or '')[:80])}:{i}:{_sha(now_iso)}" for i in range(n)]

    _hy_upsert(ids, documents, metadatas, embeddings)
    return {"inserted": n, "note": "SQLite hybrid store upsert (compat shim)"}


def _norm_url(u: str) -> str:
    """
    URL 정규화(중복 체크용)
    - 공백 제거
    - fragment(#...) 제거
    - trailing 공백 제거
    ※ querystring은 유지(같은 기사라도 파라미터가 다르면 다른 URL일 수 있어서)
    """
    u = (u or "").strip()
    if not u:
        return ""
    u = u.split("#", 1)[0].strip()
    return u


def indexto_chroma_safe(
    question: str,
    answer: str,
    news_list: List[Dict[str, Any]],
) -> Dict[str, object]:
    """
    ✅ MAX LEGAL-SAFE:
    - 뉴스는 메타(제목/URL/출처/발행일/짧은 snippet)만 저장/인덱싱
    - 외부 기사 본문(news_body) 저장/인덱싱 금지
    - 답변 내 링크 크롤링 금지
    - ✅ 추가: 중복 스킵
      * URL 있으면: url_exists(url)
      * URL 없으면: title_source_exists(title, source)
      * (추가) 같은 요청 내 중복도 in-memory로 1차 스킵
    """
    try:
        from ragapp.services.vdb_store import (
            vdb_url_exists as vector_url_exists,
            vdb_title_source_exists as vector_title_source_exists,
        )  # type: ignore
    except Exception:
        vector_url_exists = None  # type: ignore
        vector_title_source_exists = None  # type: ignore

    size = int(getattr(settings, "EMBED_CHUNK_SIZE", os.environ.get("EMBED_CHUNK_SIZE", "1600")))
    overlap = int(getattr(settings, "EMBED_CHUNK_OVERLAP", os.environ.get("EMBED_CHUNK_OVERLAP", "200")))
    now_iso = datetime.utcnow().isoformat()

    ids: List[str] = []
    docs: List[str] = []
    metas: List[Dict[str, Any]] = []

    seen_urls: set[str] = set()
    seen_ts: set[str] = set()  # title|source

    def _norm_ws(s: str) -> str:
        s = (s or "").strip().lower()
        s = " ".join(s.split())
        return s

    # A) 모델 answer → 청크
    if answer:
        ans_chunks = _chunk_text(answer, size=size, overlap=overlap)
        base_a = f"answer:{_sha(question)}"
        for i, ch in enumerate(ans_chunks):
            ch_s = (ch or "").strip()
            if not ch_s:
                continue
            ids.append(f"{base_a}:{i}")
            docs.append(ch_s)
            metas.append(
                {
                    "source": "web_answer",
                    "title": "웹검색 답변",
                    "question": question,
                    "ingested_at": now_iso,
                }
            )

    # B) 뉴스 → meta-only 인덱싱 (+ 중복 스킵)
    news_meta_only_count = 0
    skipped_dup_url = 0
    skipped_dup_title_source = 0

    for art in (news_list or []):
        url0 = (art.get("url") or "").strip()
        src0 = (art.get("source") or art.get("source_name") or "").strip()

        title0 = (art.get("title") or "").strip() or (urlparse(url0).netloc if url0 else "뉴스")
        published_at0 = (art.get("published_at") or "").strip()

        # 1) URL 있으면 URL 기준 중복 스킵
        if url0:
            u_norm = _norm_url(url0).lower()
            if u_norm:
                if u_norm in seen_urls:
                    skipped_dup_url += 1
                    continue
                seen_urls.add(u_norm)

                if callable(vector_url_exists):
                    try:
                        if vector_url_exists(url0):
                            skipped_dup_url += 1
                            continue
                    except Exception:
                        pass

        # 2) URL 없으면 title+source 기준 중복 스킵
        else:
            t_norm = _norm_ws(title0)
            s_norm = _norm_ws(src0)
            if t_norm and s_norm:
                sig = f"{t_norm}|{s_norm}"
                if sig in seen_ts:
                    skipped_dup_title_source += 1
                    continue
                seen_ts.add(sig)

                if callable(vector_title_source_exists):
                    try:
                        if vector_title_source_exists(title0, src0, published_at=published_at0):
                            skipped_dup_title_source += 1
                            continue
                    except Exception:
                        pass

        snippet_short = _clean_text_for_preview(
            "",
            fallback_snippet=(art.get("snippet") or "").strip(),
            max_len=NEWS_SNIPPET_MAX_CHARS,
        )

        id_seed = (url0 if url0 else f"{title0}::{src0}")
        base = f"news:{_slug(title0)}:{_sha(id_seed)}"

        meta_lines = [
            f"[META ONLY] {title0}",
            f"URL: {url0}" if url0 else "URL: (없음)",
            f"출처: {src0}",
            f"게시: {_iso(art.get('published_at'))}",
            snippet_short,
            "(fulltext=disabled)",
        ]
        meta_doc = "\n".join([ln for ln in meta_lines if ln]).strip()

        if meta_doc:
            ids.append(f"{base}:meta")
            docs.append(meta_doc)
            metas.append(
                {
                    "source": "news",
                    "meta_only": True,
                    "url": url0,
                    "title": title0,
                    "source_name": src0,
                    "published_at": published_at0,
                    "ingested_at": now_iso,
                    "body_len": 0,
                }
            )
            news_meta_only_count += 1

    clean = [
        (idv, docv, metav)
        for (idv, docv, metav) in zip(ids, docs, metas)
        if docv and isinstance(docv, str) and docv.strip()
    ]

    if not clean:
        return {
            "status": "ok",
            "inserted": 0,
            "answer_chunks": 0,
            "news_total_chunks": 0,
            "news_meta_only_chunks": news_meta_only_count,
            "skipped_dup_url": skipped_dup_url,
            "skipped_dup_title_source": skipped_dup_title_source,
            "engine": "vector_db",
            "vector_db_path": _VECTOR_DB_PATH,
            "collection": None,
            "dir": None,
            "ingested_at": now_iso,
            "note": "인덱싱할 데이터가 없습니다.",
        }

    ids2, docs2, metas2 = map(list, zip(*clean))
    chroma_upsert(ids=ids2, documents=docs2, metadatas=metas2)

    try:
        from ragapp.services.sqlite_hybrid_store import maybe_sync_db_to_remote_snapshot
        maybe_sync_db_to_remote_snapshot(force=True)
    except Exception as e:
        log.warning("vector_store remote sync 실패(무시하고 계속): %s", e)

    ans_chunks = sum(1 for m in metas2 if m.get("source") == "web_answer")
    news_chunks = sum(1 for m in metas2 if m.get("source") == "news")

    return {
        "status": "ok",
        "inserted": len(ids2),
        "answer_chunks": ans_chunks,
        "news_total_chunks": news_chunks,
        "news_meta_only_chunks": news_meta_only_count,
        "skipped_dup_url": skipped_dup_url,
        "skipped_dup_title_source": skipped_dup_title_source,
        "engine": "vector_db",
        "vector_db_path": _VECTOR_DB_PATH,
        "collection": None,
        "dir": None,
        "ingested_at": now_iso,
    }


# =============================================================================
# 3) RAG 검색/답변 (SQLite Hybrid Store)
# =============================================================================

def _normalize_where_filter(v) -> Optional[Dict[str, Any]]:
    if v is None:
        return None
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                d = json.loads(s)
                return d if isinstance(d, dict) else None
            except Exception:
                pass
        parts = [x.strip() for x in s.split(",") if x.strip()]
        if not parts:
            return None
        if len(parts) == 1:
            return {"source": parts[0]}
        return {"source": {"$in": parts}}
    if isinstance(v, (list, tuple, set)):
        vals = [str(x).strip() for x in v if str(x).strip()]
        if not vals:
            return None
        if len(vals) == 1:
            return {"source": vals[0]}
        return {"source": {"$in": vals}}
    return None


def _chroma_query_with_embeddings(
    col,  # 시그니처 호환
    query: str,
    topk: int,
    where: Optional[Dict[str, Any]] = None,
    include: Optional[List[str]] = None,
):
    _ = (col, include)

    q = (query or "").strip()
    if not q:
        return {"documents": [[]], "metadatas": [[]], "distances": [[]], "ids": [[]]}

    where_fixed = _normalize_where_filter(where)

    q_emb = _embed_texts([q])[0]

    try:
        return _hy_query(
            query_text=q,
            q_emb=q_emb,
            topk=int(topk),
            where=where_fixed,
            vec_topk=int(topk),
            kw_topk=int(topk),
        )
    except Exception as e:
        log.warning("_hy_query 실패 → vector-only 폴백: %s", e)
        return _hy_vec_query(q_emb=q_emb, topk=int(topk), where=where_fixed)


def _attach_faq_hits(question: str, hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if get_faq_candidates is None:
        return hits

    try:
        faq_cands = get_faq_candidates(question, top_k=3)
    except Exception as e:
        log.warning("get_faq_candidates 실패: %s", e)
        return hits

    if not faq_cands:
        return hits

    merged: List[Dict[str, Any]] = list(hits or [])
    seen = set()

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


def _maybe_override_with_faq_answer(question: str, answer_text: str) -> str:
    if get_faq_candidates is None:
        return answer_text

    try:
        faq_best_list = get_faq_candidates(question, top_k=1)
    except Exception:
        return answer_text

    if not faq_best_list:
        return answer_text

    best = faq_best_list[0]
    try:
        score = float(best.get("score", 0.0))
    except Exception:
        score = 0.0

    if score >= 0.85 and best.get("a"):
        return best["a"]

    return answer_text


def _parse_hits_from_res(res):
    def _pick(v):
        return v[0] if (isinstance(v, list) and v and isinstance(v[0], list)) else (v or [])

    docs = _pick(res.get("documents"))
    metas = _pick(res.get("metadatas"))
    ids_ = _pick(res.get("ids")) if "ids" in res else [""] * len(docs)
    dists = _pick(res.get("distances"))

    hits = []
    for i, doc in enumerate(docs):
        if not doc:
            continue
        snippet = ((doc[:800] if isinstance(doc, str) else str(doc)).replace("\n", " ").strip())
        m = metas[i] if i < len(metas) else {}
        try:
            score = float(dists[i]) if (dists and i < len(dists) and dists[i] is not None) else None
        except Exception:
            score = None
        hits.append(
            {
                "id": ids_[i] if i < len(ids_) else "",
                "score": score,
                "meta": m,
                "snippet": snippet,
            }
        )
    return hits


def _rank_and_dedupe_hits(hits: List[Dict[str, Any]], max_n: int = 8) -> List[Dict[str, Any]]:
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

    # ✅ score=distance(작을수록 좋음)
    ordered = sorted(hits, key=score_of)
    seen = set()
    out: List[Dict[str, Any]] = []
    for h in ordered:
        k = key_of(h)
        if k in seen:
            continue
        seen.add(k)
        out.append(h)
        if len(out) >= max_n:
            break
    return out


def _format_history_for_prompt(history: List[dict]) -> str:
    """
    히스토리를 프롬프트에 붙일 때만 사용(기본 OFF).
    과도한 길이를 제한.
    """
    if not history:
        return ""
    lines: List[str] = []
    for turn in history[-6:]:
        if not isinstance(turn, dict):
            continue
        role = (turn.get("role") or "").strip() or "user"
        content = (turn.get("content") or turn.get("text") or "").strip()
        if not content:
            continue
        lines.append(f"- ({role}) {content}")
    block = "\n".join(lines).strip()
    if len(block) > RAG_HISTORY_MAX_CHARS:
        block = block[:RAG_HISTORY_MAX_CHARS].rstrip() + "…"
    return block


def _build_source_block(hits: List[Dict[str, Any]]) -> str:
    budget = max(400, int(RAG_SOURCE_BLOCK_MAX_CHARS))
    out: List[str] = []

    for i, h in enumerate(hits, start=1):
        if not isinstance(h, dict):
            continue

        # ✅ 모델에게 실제로 보여준 근거 번호를 hit 자체에 보존
        # 답변의 [3]과 화면의 #3을 맞추기 위한 핵심 값
        h["citation_idx"] = i
        h["idx"] = i

        m = h.get("meta") or {}
        if not isinstance(m, dict):
            m = {}

        title = (
            m.get("title")
            or m.get("file_name")
            or m.get("url")
            or h.get("title")
            or h.get("url")
            or "문서"
        )
        title = str(title or "문서").strip()

        url = str(m.get("url") or h.get("url") or "").strip()
        netloc = urlparse(url).netloc if url else ""

        source_name = (
            m.get("source_name")
            or m.get("source")
            or h.get("source")
            or netloc
            or m.get("file_name")
            or ""
        )
        source_name = str(source_name or "").strip()

        base_head = f"[{i}] {title} · {source_name}\n"
        base_len = len(base_head) + 2

        remaining = budget - sum(len(x) + 2 for x in out)
        if remaining <= base_len + RAG_SOURCE_SNIPPET_MIN_CHARS:
            break

        max_snip = min(
            int(RAG_SOURCE_SNIPPET_MAX_CHARS),
            max(RAG_SOURCE_SNIPPET_MIN_CHARS, remaining - base_len - 2),
        )

        snippet = (
            h.get("snippet")
            or h.get("text")
            or h.get("chunk")
            or h.get("content")
            or ""
        )
        snippet = str(snippet or "").strip().replace("\n", " ")
        snippet = snippet[:max_snip].rstrip()

        out.append(f"{base_head}{snippet}")

        if sum(len(x) + 2 for x in out) >= budget:
            break

    return "\n\n".join(out).strip()

_CITATION_RE = re.compile(r"\[(\d{1,2})\]")


def _filter_hits_cited_by_answer(answer: str, hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    답변에 실제로 인용된 [1], [2], [3] 번호의 근거만 반환한다.
    인용 번호가 없으면 '답변에 사용된 근거'라고 볼 수 없으므로 빈 리스트를 반환한다.
    """
    if not hits:
        return []

    used_nums: List[int] = []

    for m in _CITATION_RE.finditer(answer or ""):
        try:
            n = int(m.group(1))
        except Exception:
            continue

        # [2024] 같은 숫자 오인 방지
        if 1 <= n <= 99 and n not in used_nums:
            used_nums.append(n)

    if not used_nums:
        fallback_n = int(getattr(settings, "RAG_EVIDENCE_FALLBACK_COUNT", 1))
        return hits[:max(0, fallback_n)]

    selected: List[Dict[str, Any]] = []

    for pos, h in enumerate(hits, start=1):
        if not isinstance(h, dict):
            continue

        try:
            citation_idx = int(
                h.get("citation_idx")
                or h.get("idx")
                or ((h.get("meta") or {}).get("citation_idx") if isinstance(h.get("meta"), dict) else None)
                or ((h.get("meta") or {}).get("idx") if isinstance(h.get("meta"), dict) else None)
                or pos
            )
        except Exception:
            citation_idx = pos

        if citation_idx in used_nums:
            selected.append(h)

    return selected

def _fix_invalid_citations(answer: str, hits: List[Dict[str, Any]]) -> str:
    """
    LLM이 실제 존재하지 않는 근거 번호([2], [3] 등)를 붙인 경우
    실제 존재하는 첫 번째 근거 번호로 보정한다.
    """
    clean_hits = [h for h in (hits or []) if isinstance(h, dict)]

    if not clean_hits:
        return _CITATION_RE.sub("", answer or "").strip()

    valid_nums: set[int] = set()

    for pos, h in enumerate(clean_hits, start=1):
        try:
            valid_nums.add(
                int(
                    h.get("citation_idx")
                    or h.get("idx")
                    or ((h.get("meta") or {}).get("citation_idx") if isinstance(h.get("meta"), dict) else None)
                    or ((h.get("meta") or {}).get("idx") if isinstance(h.get("meta"), dict) else None)
                    or pos
                )
            )
        except Exception:
            valid_nums.add(pos)

    first_num = min(valid_nums) if valid_nums else 1

    def repl(m):
        try:
            n = int(m.group(1))
        except Exception:
            return f"[{first_num}]"

        if n in valid_nums:
            return f"[{n}]"

        return f"[{first_num}]"

    return _CITATION_RE.sub(repl, answer or "")


def _make_rag_prompt(question: str, source_block: str, hard: bool = False, history_block: str = "") -> str:
    hist = ""
    if history_block:
        hist = f"[대화 요약]\n{history_block}\n\n"
    if hard:
        return (
            "아래 '근거 자료'에 있는 내용만 사용해 한국어로 핵심을 정리해 답하세요.\n"
            "- 문장/항목 끝에 반드시 [1], [2]처럼 근거 번호 인용을 붙이세요.\n"
            "- 직접적 근거가 부족하면 '자료 내 직접 근거 부족' 한 줄만 쓰고 추측은 금지합니다.\n"
            "- 군더더기 없이 핵심만 요약하세요.\n\n"
            f"{hist}"
            f"[질문]\n{question}\n\n[근거 자료]\n{source_block}\n\n=== 답변 시작 ===\n"
        )
    return (
        "아래 '근거 자료'를 최우선으로 참고해 한국어로 4~8문장으로 핵심을 답하세요.\n"
        "- 근거 자료를 사용한 문장 끝에는 반드시 [1], [2]처럼 근거 번호를 붙이세요.\n"
        "- 직접적 근거가 부족한 내용은 '자료 내 직접 근거 부족'이라고 표시하세요.\n"
        "- 일반 지식으로 보완한 문장에는 근거 번호를 붙이지 마세요.\n"
        "- 불필요한 서론 없이 핵심만.\n\n"
        f"{hist}"
        f"[질문]\n{question}\n\n[근거 자료]\n{source_block}\n\n=== 답변 시작 ===\n"
    )


# =============================================================================
# ✅ 여기부터: vdb_query_hybrid 결과를 hits로 바꾸는 “정식” 경로
# =============================================================================

def _embed_query_text(q: str) -> List[float]:
    """
    질문 1개 임베딩 벡터 생성.

    ✅ 정리 포인트:
    - vertex_embed.embed_texts가 있으면 그걸 우선 재사용
    - 없으면 이 파일의 _embed_texts()를 사용
    - news_services를 다시 import 하는 폴백은 제거(순환 위험)
    """
    q0 = (q or "").strip()
    if not q0:
        return []

    try:
        from ragapp.services.vertex_embed import embed_texts as _embed_texts_ext  # type: ignore
        vecs = _embed_texts_ext([q0])
        return list(vecs[0]) if vecs else []
    except Exception:
        vecs = _embed_texts([q0])
        return list(vecs[0]) if vecs else []


def _vdb_query_with_embeddings(*, query_text: str, topk: int, where=None) -> Dict[str, Any]:
    """
    sqlite_hybrid_store(query_hybrid)에 위임.
    - where는 _normalize_where_filter로 정규화
    - vec_topk/kw_topk는 None 금지(후보 0 되는 케이스 방지)
    """
    from ragapp.services.vdb_store import vdb_query_hybrid, vdb_query_vector_only  # type: ignore

    q = (query_text or "").strip()
    if not q:
        return {"documents": [[]], "metadatas": [[]], "distances": [[]], "ids": [[]]}

    where_fixed = _normalize_where_filter(where)

    q_emb = _embed_query_text(q)
    if not q_emb:
        return {"documents": [[]], "metadatas": [[]], "distances": [[]], "ids": [[]]}

    k = max(1, int(topk))
    # 후보 풀을 조금 넓게 잡는 게 안정적
    vec_k = max(k, k * 2)
    kw_k = max(k, k * 2)

    try:
        return vdb_query_hybrid(
            query_text=q,
            q_emb=q_emb,
            topk=k,
            where=where_fixed,
            vec_topk=vec_k,
            kw_topk=kw_k,
        )
    except Exception as e:
        log.warning("_vdb_query_with_embeddings: hybrid 실패 → vector-only 폴백: %s", e)
        return vdb_query_vector_only(q_emb=q_emb, topk=k, where=where_fixed)


def _parse_hits_from_vdb_res(res: Any) -> List[Dict[str, Any]]:
    """
    vdb_query_hybrid 결과를 hits(list[dict])로 변환.
    hits 형태:
      {"id":..., "meta":{...}, "snippet": "...", "score": ...}

    ✅ score는 distance(작을수록 유사)로 통일.
    ✅ dedupe 강화를 위해 meta.url/title이 비어 있으면 보강.
    """
    if not isinstance(res, dict):
        return []

    docs = res.get("documents") or []
    metas = res.get("metadatas") or []
    dists = res.get("distances") or []
    ids = res.get("ids") or []

    docs0 = docs[0] if isinstance(docs, list) and docs else []
    metas0 = metas[0] if isinstance(metas, list) and metas else []
    dists0 = dists[0] if isinstance(dists, list) and dists else []
    ids0 = ids[0] if isinstance(ids, list) and ids else []

    out: List[Dict[str, Any]] = []
    n = max(len(docs0), len(metas0), len(dists0), len(ids0))

    for i in range(n):
        doc = docs0[i] if i < len(docs0) else ""
        meta0 = metas0[i] if i < len(metas0) else {}
        dist = dists0[i] if i < len(dists0) else None
        vid = ids0[i] if i < len(ids0) else ""

        meta = dict(meta0) if isinstance(meta0, dict) else {}

        text = (doc or "").strip()
        if not text:
            continue

        score = 1e9
        try:
            if dist is not None:
                score = float(dist)
        except Exception:
            score = 1e9

        if not meta.get("title"):
            meta["title"] = meta.get("file_name") or meta.get("doc_id") or "문서"

        if not meta.get("url"):
            doc_id = (meta.get("doc_id") or meta.get("vdb_id") or vid or "").strip()
            if doc_id:
                meta["url"] = f"doc://{doc_id}"

        out.append(
            {
                "id": str(vid or meta.get("vdb_id") or meta.get("doc_id") or ""),
                "meta": meta,
                "snippet": text,
                "score": score,
            }
        )

    return out


def rag_answer_grounded(
    question: str,
    initial_topk: int = 5,
    fallback_topk: int = 12,
    max_sources: int = 8,
) -> Tuple[str, List[Dict[str, Any]]]:

    sources_filter = getattr(settings, "RAG_SOURCES_FILTER", None)

    rag_force_answer = getattr(
        settings,
        "RAG_FORCE_ANSWER",
        os.environ.get("RAG_FORCE_ANSWER", "1").lower() not in ("0", "false", "no"),
    )

    # (선택) vdb 상태 로그
    try:
        from ragapp.services.vdb_store import vdb_info  # type: ignore
        log.info("[rag] vdb_info=%s filter=%s", vdb_info(), sources_filter)
    except Exception:
        pass

    # 1차 검색(필터 적용)
    res1 = _vdb_query_with_embeddings(query_text=question, topk=initial_topk, where=sources_filter)
    hits1_all = _parse_hits_from_vdb_res(res1)
    hits1 = _rank_and_dedupe_hits(hits1_all, max_sources)
    block1 = _build_source_block(hits1)

    ans1 = ask_gemini(_make_rag_prompt(question, block1, hard=not rag_force_answer), model=None)

    def _weak(a: str) -> bool:
        t = (a or "").strip()
        return (not t) or (len(t) < 120) or (t == _EMPTY_FALLBACK)

    if not _weak(ans1):
        hits1_fixed = _attach_faq_hits(question, hits1)
        ans1_fixed = _maybe_override_with_faq_answer(question, ans1)
        ans1_fixed = _fix_invalid_citations(ans1_fixed, hits1_fixed)
        return ans1_fixed, _filter_hits_cited_by_answer(ans1_fixed, hits1_fixed)

    # 2차: 키워드 확장(모델 1회 추가 호출)
    try:
        kw = ask_gemini(
            "아래 질문의 한국어 핵심 키워드를 쉼표로 10개만. 설명 없이 키워드만:\n" + question,
            model=None,
        )
    except Exception:
        kw = ""

    expanded_q = (question + " " + (kw or "")).strip()

    # 2차 검색(필터 없이 넓게)
    res2 = _vdb_query_with_embeddings(query_text=expanded_q, topk=fallback_topk, where=None)
    hits2_all = _parse_hits_from_vdb_res(res2)
    hits2 = _rank_and_dedupe_hits(hits2_all, max_sources)
    block2 = _build_source_block(hits2)

    ans2 = ask_gemini(_make_rag_prompt(question, block2, hard=not rag_force_answer), model=None)

    if not _weak(ans2):
        hits2_fixed = _attach_faq_hits(question, hits2)
        ans2_fixed = _maybe_override_with_faq_answer(question, ans2)
        ans2_fixed = _fix_invalid_citations(ans2_fixed, hits2_fixed)
        return ans2_fixed, _filter_hits_cited_by_answer(ans2_fixed, hits2_fixed)

    # 3차: 강제 답변(설정 ON일 때만)
    if rag_force_answer and _weak(ans2):
        ans_fallback = ask_gemini(
            "다음 질문에 대해 일반 지식과 상식을 바탕으로 한국어로 4~8문장 핵심 요약 답을 작성하세요. "
            "군더더기 금지, 안전하고 중립적인 표현 사용:\n\n"
            f"{question}\n\n=== 답변 시작 ===\n",
            model=None,
        )
        if (ans_fallback or "").strip():
            ans_fb_fixed = _maybe_override_with_faq_answer(question, ans_fallback.strip())
            return ans_fb_fixed, []

    final_ans = (ans2 or ans1 or _EMPTY_FALLBACK)
    final_hits = (hits2 or hits1)

    final_hits = _attach_faq_hits(question, final_hits)
    final_ans = _maybe_override_with_faq_answer(question, final_ans)
    final_ans = _fix_invalid_citations(final_ans, final_hits)

    return final_ans, _filter_hits_cited_by_answer(final_ans, final_hits)


def _rerank_hits_by_relevance(question: str, hits: List[Dict[str, Any]], topn: int = 5):
    _ = question
    if not hits:
        return []

    def score(h):
        s = h.get("score")
        try:
            return float(s) if s is not None else 1e9
        except Exception:
            return 1e9

    return sorted(hits, key=score)[:topn]


def rag_answer_grounded_with_history(
    question: str,
    history: List[dict],
    *,
    base_retriever_func=rag_answer_grounded,
    initial_topk: int = 5,
    fallback_topk: int = 12,
    max_sources: int = 8,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    기본 동작은 기존과 동일(검색/생성 자체는 base_retriever_func에 위임).
    - 최적화 관점에서 "history를 프롬프트에 섞는 기능"은 옵션으로만 제공(기본 OFF).
    """
    if not RAG_USE_HISTORY_IN_PROMPT:
        _ = history
        answer_text, used_hits = base_retriever_func(
            question,
            initial_topk=initial_topk,
            fallback_topk=fallback_topk,
            max_sources=max_sources,
        )
        # ✅ 답변 생성 당시 근거 번호(citation_idx)를 유지해야 하므로
        # 여기서 다시 정렬하지 않는다.
        return answer_text, used_hits

    history_block = _format_history_for_prompt(history)
    answer_text, used_hits = base_retriever_func(
        question,
        initial_topk=initial_topk,
        fallback_topk=fallback_topk,
        max_sources=max_sources,
    )

    # ✅ 답변 생성 후 근거 순서를 다시 정렬하면
    # 답변의 [1], [2], [3] 번호와 화면의 #번호가 어긋날 수 있음
    # ✅ 원본 used_hits의 citation_idx를 직접 덮어쓰지 않도록 복사
    final_hits = [dict(h) if isinstance(h, dict) else h for h in (used_hits or [])]

    try:
        block = _build_source_block(final_hits)
        refined = ask_gemini(
            _make_rag_prompt(
                question=question,
                source_block=block,
                hard=False,
                history_block=history_block,
            ),
            model=None,
        )
        if refined and refined.strip():
            answer_text = refined.strip()
            return answer_text, _filter_hits_cited_by_answer(answer_text, final_hits)
    except Exception:
        pass

    return answer_text, used_hits


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
            if not isinstance(m, dict):
                m = {}

            try:
                citation_idx = int(
                    h.get("citation_idx")
                    or h.get("idx")
                    or m.get("citation_idx")
                    or m.get("idx")
                    or i
                )
            except Exception:
                citation_idx = i

            hits_payload.append(
                {
                    "idx": citation_idx,
                    "citation_idx": citation_idx,
                    "title": (
                        m.get("title")
                        or m.get("url")
                        or h.get("title")
                        or h.get("url")
                        or "문서"
                    ),
                    "source_type": (
                        m.get("source")
                        or h.get("source")
                        or ""
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
                    "citation_idx": i,
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


__all__ = [
    # 생성/임베딩
    "ask_gemini",
    "_embed_texts",
    # 뉴스(메타 only)
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
    "run_rag_qa",
    # 헬퍼(호환)
    "_chunk_text",
    "_slug",
    "_sha",
    "_iso",
    "_chroma_collection",
    "_chroma_query_with_embeddings",
]
