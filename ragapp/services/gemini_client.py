# ragapp/services/gemini_client.py
from __future__ import annotations
import os
import inspect
from typing import List, Optional, Any

from django.conf import settings

# google-genai (통합 SDK)
try:
    from google import genai
except Exception as _e:  # pragma: no cover
    genai = None  # 런타임에서 에러 메시지로 안내

LAST_EMBED_META = {"param": None, "model": None, "dim": None}


# ─────────────────────────────────────────────────────────────────────────────
# 공통 유틸
# ─────────────────────────────────────────────────────────────────────────────
def _as_bool(v) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s not in ("0", "false", "no", "")


def _want_vertex_ai() -> bool:
    """
    Vertex 전용 모드 여부.
    - 기본 True (GOOGLE_GENAI_USE_VERTEXAI, USE_VERTEX_AI 중 하나라도 true면 확정)
    """
    # settings.py 에서 GOOGLE_GENAI_USE_VERTEXAI 기본값을 true 로 깔아두었으므로,
    # 별다른 설정이 없으면 Vertex 경로를 사용한다.
    if _as_bool(os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "true")):
        return True
    if _as_bool(getattr(settings, "USE_VERTEX_AI", None) or os.environ.get("USE_VERTEX_AI")):
        return True
    return True  # 안전하게 Vertex 고정


def _vertex_params():
    """
    Vertex 파라미터 (settings/env만 사용; API Key는 사용하지 않음, ADC/서비스 계정 JSON 전용)
    - project:
        settings.VERTEX_PROJECT_ID
        > env.VERTEX_PROJECT_ID
        > env.GCP_PROJECT / GOOGLE_CLOUD_PROJECT / GCLOUD_PROJECT
    - location:
        settings.VERTEX_LOCATION
        > env.VERTEX_LOCATION / GCP_LOCATION / GOOGLE_CLOUD_LOCATION
        > 'us-central1'
    - api_version:
        settings.GENAI_API_VERSION
        > env.GENAI_API_VERSION / GEMINI_API_VERSION
        > 'v1'
    """
    project = (
        getattr(settings, "VERTEX_PROJECT_ID", None)
        or os.environ.get("VERTEX_PROJECT_ID")
        or os.environ.get("GCP_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GCLOUD_PROJECT")
    )
    location = (
        getattr(settings, "VERTEX_LOCATION", None)
        or os.environ.get("VERTEX_LOCATION")
        or os.environ.get("GCP_LOCATION")
        or os.environ.get("GOOGLE_CLOUD_LOCATION")
        or "us-central1"
    )
    api_version = (
        getattr(settings, "GENAI_API_VERSION", None)
        or os.environ.get("GENAI_API_VERSION")
        or os.environ.get("GEMINI_API_VERSION")
        or "v1"
    )

    if not project:
        raise RuntimeError(
            "Vertex AI 모드: VERTEX_PROJECT_ID (또는 GCP_PROJECT/GOOGLE_CLOUD_PROJECT/GCLOUD_PROJECT)가 필요합니다."
        )

    return project, location, api_version


def _http_options_or_none():
    """
    google-genai 버전에 따라 HttpOptions 경로가 다를 수 있으므로 안전 획득
    """
    if genai is None:
        return None
    try:
        # 최신 버전 경로
        return genai.types.HttpOptions  # type: ignore[attr-defined]
    except Exception:
        try:
            # 일부 배포판/버전 호환
            from google.genai.types import HttpOptions  # type: ignore
            return HttpOptions
        except Exception:
            return None


def _gemini_client():
    """
    Vertex + ADC(서비스 계정 JSON / gcloud 로그인 등) 전용 클라이언트 생성.

    - API Key 는 전혀 사용하지 않는다.
    - 인증은 GOOGLE_APPLICATION_CREDENTIALS, gcloud auth, GCE 메타데이터 등
      표준 ADC(Application Default Credentials)를 따른다.
    """
    if genai is None:
        raise RuntimeError("google-genai 미설치: pip install -U google-genai")

    if not _want_vertex_ai():
        raise RuntimeError("Vertex-only 모드인데 USE_VERTEX_AI 플래그가 비활성화되어 있습니다. 환경변수를 확인하세요.")

    project, location, api_version = _vertex_params()
    HttpOptions = _http_options_or_none()

    if HttpOptions:
        # HttpOptions 지원 버전
        return genai.Client(  # type: ignore[call-arg]
            vertexai=True,
            project=project,
            location=location,
            http_options=HttpOptions(api_version=api_version),
        )

    # HttpOptions 미제공 버전 폴백
    return genai.Client(  # type: ignore[call-arg]
        vertexai=True,
        project=project,
        location=location,
    )


def _require_env_model_text() -> str:
    """
    텍스트 생성 모델은 .env에서만 고른다.
    우선순위:
      GEMINI_MODEL
      → GEMINI_MODEL_DIRECT
      → GEMINI_TEXT_MODEL
      → VERTEX_TEXT_MODEL
      → GEMINI_MODEL_DEFAULT
    (없으면 예외)
    """
    for k in ["GEMINI_MODEL", "GEMINI_MODEL_DIRECT", "GEMINI_TEXT_MODEL", "VERTEX_TEXT_MODEL", "GEMINI_MODEL_DEFAULT"]:
        v = os.environ.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    raise RuntimeError(
        "텍스트 모델이 없습니다. .env에 GEMINI_MODEL=gemini-2.5-flash 등 원하는 모델명을 설정하세요."
    )


def _embed_models_from_env() -> list[str]:
    """
    임베딩 모델 후보를 .env에서만 읽어 리스트로 반환.
    우선순위:
      VERTEX_EMBED_MODEL (단일)
      → GEMINI_EMBED_MODELS(쉼표)
      → GEMINI_EMBED_MODEL(단일)
    (없으면 예외)
    """
    raw: Optional[str] = None
    if os.environ.get("VERTEX_EMBED_MODEL"):
        raw = os.environ["VERTEX_EMBED_MODEL"].strip()
    elif os.environ.get("GEMINI_EMBED_MODELS"):
        raw = os.environ["GEMINI_EMBED_MODELS"].strip()
    elif os.environ.get("GEMINI_EMBED_MODEL"):
        raw = os.environ["GEMINI_EMBED_MODEL"].strip()

    if not raw:
        raise RuntimeError(
            "임베딩 모델이 없습니다. .env에 VERTEX_EMBED_MODEL=text-embedding-004 "
            "또는 GEMINI_EMBED_MODELS=text-embedding-004 를 설정하세요."
        )

    items = [x.strip() for x in raw.split(",") if x.strip()]
    return items or []


# ─────────────────────────────────────────────────────────────────────────────
# 텍스트 생성
# ─────────────────────────────────────────────────────────────────────────────
def ask_gemini(prompt: str, model: Optional[str] = None) -> str:
    """
    텍스트 생성 호출 (Vertex + ADC 전용)
    - 모델은 무조건 .env에서 선택 (model 인자 무시)
    - generate_content 파라미터명(contents/content/input) 자동 호환
    """
    try:
        c = _gemini_client()
        mdl = _require_env_model_text()  # ★ env 강제

        # 파라미터명 호환
        try:
            sig = inspect.signature(c.models.generate_content)  # type: ignore[attr-defined]
            if "contents" in sig.parameters:
                param = "contents"
            elif "content" in sig.parameters:
                param = "content"
            elif "input" in sig.parameters:
                param = "input"
            else:
                param = "contents"
        except Exception:
            param = "contents"

        kwargs = {"model": mdl, param: prompt}
        r = c.models.generate_content(**kwargs)  # type: ignore[attr-defined]

        # 통합 SDK 응답 파싱
        txt = getattr(r, "text", None)
        if not txt and getattr(r, "candidates", None):
            try:
                # candidates → content.parts[*].text 모아 붙이기
                parts = []
                for cand in r.candidates or []:
                    content = getattr(cand, "content", None)
                    if content and getattr(content, "parts", None):
                        for p in content.parts:
                            t = getattr(p, "text", None) or (str(p) if p is not None else "")
                            if t:
                                parts.append(t)
                if parts:
                    txt = "\n".join(parts)
            except Exception:
                pass
        return (txt or "").strip() or "[빈 응답]"
    except Exception as e:
        return f"[모델 응답 실패: {e}]"


# ─────────────────────────────────────────────────────────────────────────────
# 임베딩 (배치 안전)
# ─────────────────────────────────────────────────────────────────────────────
def _parse_embedding(resp: Any) -> Optional[List[float]]:
    """
    google-genai 응답의 다양한 모양을 안전 파싱
    """
    # attr 스타일
    try:
        emb = getattr(resp, "embedding", None)
        if emb is not None:
            vals = getattr(emb, "values", None)
            if vals:
                return [float(x) for x in vals]
    except Exception:
        pass
    try:
        embs = getattr(resp, "embeddings", None)
        if embs:
            first = embs[0] if len(embs) else None
            if first is not None:
                vals = getattr(first, "values", None)
                if vals:
                    return [float(x) for x in vals]
    except Exception:
        pass

    # dict 스타일
    if isinstance(resp, dict):
        try:
            vals = resp.get("embedding", {}).get("values")
            if vals:
                return [float(x) for x in vals]
        except Exception:
            pass
        try:
            first = (resp.get("embeddings") or [None])[0]
            if isinstance(first, dict) and "values" in first:
                return [float(x) for x in first["values"]]
        except Exception:
            pass

    # data[0].embedding.values 형태
    try:
        data = getattr(resp, "data", None)
        if data:
            item = data[0]
            emb = getattr(item, "embedding", None)
            if emb is not None:
                vals = getattr(emb, "values", None)
                if vals:
                    return [float(x) for x in vals]
    except Exception:
        pass

    return None


def embed_texts(texts: List[str]) -> List[List[float]]:
    """
    텍스트 배열 -> 임베딩 벡터 배열 (통합 SDK / Vertex + ADC 전용)
    - 모델은 무조건 .env에서만 선택
    - 우선 embeddings.create(model=..., input=...) 시도
      실패 시 models.embed_content(model=..., contents|content|input=...) 시도
    - 파라미터명(content/contents/input) 자동 호환
    """
    if not texts:
        return []

    c = _gemini_client()
    models = _embed_models_from_env()  # ★ env 강제

    errors: list[str] = []
    for model_name in models:
        # 1) embeddings.create 우선 시도
        try:
            vecs: List[List[float]] = []
            for t in texts:
                r = c.embeddings.create(model=model_name, input=t)  # type: ignore[attr-defined]
                v = _parse_embedding(r)
                if not v:
                    raise RuntimeError("임베딩 응답 파싱 실패")
                vecs.append(v)
            LAST_EMBED_META.update({"param": "input", "model": model_name, "dim": len(vecs[0])})
            return vecs
        except Exception as e1:
            errors.append(f"{model_name} via embeddings.create(input): {e1}")

        # 2) models.embed_content 폴백 (파라미터명 자동)
        try:
            # 파라미터명 호환
            try:
                sig = inspect.signature(c.models.embed_content)  # type: ignore[attr-defined]
                if "contents" in sig.parameters:
                    p = "contents"
                elif "content" in sig.parameters:
                    p = "content"
                elif "input" in sig.parameters:
                    p = "input"
                else:
                    p = "contents"
            except Exception:
                p = "contents"

            vecs2: List[List[float]] = []
            for t in texts:
                r2 = c.models.embed_content(model=model_name, **{p: t})  # type: ignore[attr-defined]
                v2 = _parse_embedding(r2)
                if not v2:
                    raise RuntimeError("임베딩 응답 파싱 실패")
                vecs2.append(v2)
            LAST_EMBED_META.update({"param": p, "model": model_name, "dim": len(vecs2[0])})
            return vecs2
        except Exception as e2:
            errors.append(f"{model_name} via embed_content({p}): {e2}")
            continue

    # 여기까지 왔다는 건 후보 모델 전부 실패한 것
    raise RuntimeError("임베딩 실패: " + " | ".join(errors))


def current_embed_dim() -> int:
    try:
        v = embed_texts(["__dim_probe__"])[0]
        return len(v)
    except Exception:
        return -1
