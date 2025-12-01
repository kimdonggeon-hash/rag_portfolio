# ragapp/services/llm_vertex.py
from __future__ import annotations

import os
import logging
from typing import List, Optional, Dict, Any

from django.conf import settings
from google import genai  # pip install google-genai>=0.3.0

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Vertex(서비스 계정) 경로로 고정된 google-genai 클라이언트
#  - 인증: GOOGLE_APPLICATION_CREDENTIALS(.env) 기반 ADC
#  - 엔드포인트: Vertex AI (project/location 필요)
# ─────────────────────────────────────────────────────────────────────────────
def _client() -> genai.Client:
    proj = (
        getattr(settings, "VERTEX_PROJECT_ID", None)
        or os.environ.get("VERTEX_PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GCP_PROJECT")
    )
    loc = (
        getattr(settings, "VERTEX_LOCATION", None)
        or os.environ.get("VERTEX_LOCATION")
        or os.environ.get("GCP_LOCATION")
        or "us-central1"
    )

    if not proj:
        raise RuntimeError("VERTEX_PROJECT_ID / GOOGLE_CLOUD_PROJECT / GCP_PROJECT 중 하나가 .env에 설정되어야 합니다.")
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS 가 .env에 설정되어야 합니다.")

    c = genai.Client(vertexai=True, project=proj, location=loc)
    log.info("google-genai (Vertex) initialized: project=%s, location=%s", proj, loc)
    return c


# ─────────────────────────────────────────────────────────────────────────────
# 모델 선택 유틸 (★ 요구사항: '무조건 .env의 모델'만 사용, 하드코딩 기본값/설정값으로 대체 금지)
# ─────────────────────────────────────────────────────────────────────────────
def _first_non_empty(vals: list[Optional[str]]) -> Optional[str]:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None

def _require_env_model_text() -> str:
    """
    텍스트 생성 모델은 .env에서만 고른다.
    우선순위: GEMINI_MODEL → GEMINI_MODEL_DIRECT → GEMINI_TEXT_MODEL → VERTEX_TEXT_MODEL → GEMINI_MODEL_DEFAULT
    (어느 것도 없으면 예외)
    """
    picked = _first_non_empty([
        os.environ.get("GEMINI_MODEL"),
        os.environ.get("GEMINI_MODEL_DIRECT"),
        os.environ.get("GEMINI_TEXT_MODEL"),
        os.environ.get("VERTEX_TEXT_MODEL"),
        os.environ.get("GEMINI_MODEL_DEFAULT"),
    ])
    if not picked:
        raise RuntimeError(
            "텍스트 모델이 없습니다. .env에 GEMINI_MODEL  (또는 원하는 모델명)을 설정하세요."
        )
    return picked

def _require_env_model_embed() -> str:
    """
    임베딩 모델은 .env에서만 고른다.
    우선순위: VERTEX_EMBED_MODEL → GEMINI_EMBED_MODELS(첫 항목) → GEMINI_EMBED_MODEL
    (어느 것도 없으면 예외)
    """
    # 1) 단일 키
    vtx = os.environ.get("VERTEX_EMBED_MODEL")
    if vtx and vtx.strip():
        return vtx.strip()

    # 2) 복수 키(쉼표 가능)
    gem_multi = os.environ.get("GEMINI_EMBED_MODELS")
    if gem_multi and gem_multi.strip():
        first = gem_multi.split(",")[0].strip()
        if first:
            return first

    # 3) 구버전/대체 키
    gem_single = os.environ.get("GEMINI_EMBED_MODEL")
    if gem_single and gem_single.strip():
        return gem_single.strip()

    raise RuntimeError(
        "임베딩 모델이 없습니다. .env에 VERTEX_EMBED_MODEL=text-embedding-004 "
        "또는 GEMINI_EMBED_MODELS=text-embedding-004 를 설정하세요."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 텍스트 생성
# ─────────────────────────────────────────────────────────────────────────────
def generate_text(
    prompt: str,
    *,
    model: Optional[str] = None,  # ★ 요구사항에 따라 무시됨(호출자가 넘겨도 .env 강제)
    generation_config: Optional[Dict[str, Any]] = None,
    safety_settings: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Vertex 경유 google-genai 텍스트 생성 (키 기반 AI Studio 비사용).
    모델은 무조건 .env에서 선택한다.
    """
    c = _client()
    mdl = _require_env_model_text()  # ← 인자/설정 무시, .env 강제

    try:
        resp = c.models.generate_content(
            model=mdl,
            contents=prompt,
            generation_config=generation_config,
            safety_settings=safety_settings,
        )
        # 통일된 파싱
        txt = getattr(resp, "text", None)
        if not txt and getattr(resp, "candidates", None):
            try:
                txt = resp.candidates[0].content.parts[0].text
            except Exception:
                pass
        return (txt or "").strip() or "[빈 응답]"
    except Exception as e:
        log.warning("generate_text 실패 (model=%s): %s", mdl, e)
        return f"[모델 응답 실패: {e}]"


# ─────────────────────────────────────────────────────────────────────────────
# 임베딩 (배치 안전)
# ─────────────────────────────────────────────────────────────────────────────
def embed_texts(texts: List[str], *, model: Optional[str] = None) -> List[List[float]]:
    """
    Vertex 경유 google-genai 임베딩.
    SDK 버전에 따라 단건/배치 시그니처가 달라 예외 안전하게 처리.
    모델은 무조건 .env에서 선택한다.
    """
    if not texts:
        return []

    c = _client()
    mdl = _require_env_model_embed()  # ← 인자/설정 무시, .env 강제

    # 1) 배치 시도
    try:
        r = c.models.embed_content(model=mdl, contents=texts)
        embs = getattr(r, "embeddings", None)
        if embs:
            return [list(e.values) for e in embs]
        if isinstance(r, dict) and "embeddings" in r:
            return [list(e["values"]) for e in r["embeddings"]]
    except Exception:
        pass

    # 2) 단건 반복(최대 호환)
    out: List[List[float]] = []
    for t in texts:
        r = c.models.embed_content(model=mdl, contents=t)
        v = None
        try:
            v = list(r.embedding.values)  # 표준 경로
        except Exception:
            try:
                v = list(r["embedding"]["values"])  # dict fallback
            except Exception:
                v = None
        if not v:
            raise RuntimeError("임베딩 응답 파싱 실패")
        out.append(v)
    return out
