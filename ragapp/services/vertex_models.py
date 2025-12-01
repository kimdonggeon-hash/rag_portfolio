# ragapp/services/vertex_models.py
from __future__ import annotations
import os
from google.genai import types
from .vertex_client import get_vertex_client

# Django settings가 있으면 활용(없어도 동작)
try:
    from django.conf import settings as _dj_settings
    _HAS_DJ_SETTINGS = True
except Exception:
    _dj_settings = None
    _HAS_DJ_SETTINGS = False


def _first_non_empty(values: list[str | None]) -> str | None:
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _choose_model(explicit: str | None) -> str:
    # 1) 함수 인자로 명시되면 최우선
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()

    # 2) Django settings에서 우선순위대로
    if _HAS_DJ_SETTINGS and _dj_settings:
        picked = _first_non_empty([
            getattr(_dj_settings, "GEMINI_MODEL", None),
            getattr(_dj_settings, "GEMINI_MODEL_DIRECT", None),
            getattr(_dj_settings, "GEMINI_TEXT_MODEL", None),
            getattr(_dj_settings, "VERTEX_TEXT_MODEL", None),
        ])
        if picked:
            return picked

    # 3) .env/환경변수에서 우선순위대로
    picked = _first_non_empty([
        os.environ.get("GEMINI_MODEL"),          # 권장: 공통 키
        os.environ.get("GEMINI_MODEL_DIRECT"),   # 사용처 분리 키(선택)
        os.environ.get("GEMINI_TEXT_MODEL"),     # 하위호환
        os.environ.get("VERTEX_TEXT_MODEL"),     # 하위호환
        os.environ.get("GEMINI_MODEL_DEFAULT"),  # (선택) 기본값 전용 키를 쓰고 싶을 때
    ])
    if picked:
        return picked

    # 4) 하드코딩 기본값 사용 금지 → .env에 지정하도록 강제
    raise RuntimeError(
        "Gemini/Vertex 모델명이 지정되지 않았습니다. "
        "'.env'에 GEMINI_MODEL (또는 원하는 모델명)을 설정하세요."
    )


def vertex_generate_text(prompt: str, model: str | None = None, **overrides) -> str:
    model = _choose_model(model)

    cfg = types.GenerateContentConfig(
        temperature=float(overrides.get("temperature", os.environ.get("VERTEX_TEMPERATURE", "0.2"))),
        max_output_tokens=int(overrides.get("max_output_tokens", os.environ.get("VERTEX_MAX_TOKENS", "2048"))),
    )

    cli = get_vertex_client()
    resp = cli.models.generate_content(model=model, contents=prompt, config=cfg)
    return (getattr(resp, "text", "") or "").strip()
