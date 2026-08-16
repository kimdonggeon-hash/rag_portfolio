# ragapp/services/vertex_embed.py
from __future__ import annotations

import os
import logging
import hashlib
import re
from math import sqrt
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


def local_embeddings_enabled() -> bool:
    return str(os.getenv("LOCAL_EMBEDDINGS", "0")).strip().lower() in {
        "1", "true", "yes", "y", "on",
    }


def embed_texts_local(texts: List[str], dim: int = 384) -> List[List[float]]:
    """Deterministic hashing embeddings for offline/local development.

    This is intentionally shared by ingestion and retrieval so newly uploaded or
    crawled content is searchable without a Vertex network call.
    """
    vectors: List[List[float]] = []
    for raw in texts:
        vec = [0.0] * dim
        normalized = re.sub(r"\s+", " ", str(raw or "").lower()).strip()
        tokens = re.findall(r"[0-9a-zA-Z가-힣]+", normalized)
        # Include Korean/Latin character trigrams so partial terms still match.
        compact = "".join(tokens)
        features = tokens + [compact[i:i + 3] for i in range(max(0, len(compact) - 2))]
        for feature in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "little") % dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[idx] += sign
        norm = sqrt(sum(value * value for value in vec))
        vectors.append([value / norm for value in vec] if norm else vec)
    return vectors

# =========================================================
# Vertex AI SDK 로드 (임베딩 + LLM)
# =========================================================
try:
    import vertexai
    from vertexai.vision_models import MultiModalEmbeddingModel, Image
    from vertexai.language_models import TextEmbeddingModel
except Exception:
    vertexai = None  # type: ignore
    MultiModalEmbeddingModel = None  # type: ignore
    Image = None  # type: ignore
    TextEmbeddingModel = None  # type: ignore

# LLM 전용 (버전이 낮아서 없어도 임베딩은 계속 동작하도록 분리)
try:
    from vertexai.generative_models import GenerativeModel  # type: ignore
except Exception:  # pragma: no cover
    GenerativeModel = None  # type: ignore

# =========================================================
# 환경 변수 (프로젝트는 VERTEX_PROJECT_ID만 사용)
# =========================================================
# ✅ 기본 GCP/Vertex 리전: 도쿄 유지
LOCATION = (os.getenv("VERTEX_LOCATION") or "").strip() or "asia-northeast1"

# ✅ Gemini LLM 전용 리전
# - gemini-3.5-flash는 asia-northeast1로 호출하면 404가 날 수 있으므로 global 우선
LLM_LOCATION = (
    os.getenv("VERTEX_LLM_LOCATION")
    or "global"
).strip()

# ✅ 텍스트 임베딩 전용 리전
# - 없으면 기존 LOCATION(도쿄)을 사용해서 기존 동작 최대한 유지
EMBED_LOCATION = (
    os.getenv("VERTEX_EMBED_LOCATION")
    or os.getenv("EMBED_LOCATION")
    or LOCATION
).strip()

# ✅ 멀티모달 임베딩 전용 리전
# - 없으면 임베딩 리전 사용
MM_LOCATION = (
    os.getenv("VERTEX_MM_LOCATION")
    or os.getenv("VERTEX_MULTIMODAL_LOCATION")
    or EMBED_LOCATION
).strip()

# 임베딩 모델 이름 (.env 만 보고 결정)
MM_MODEL = (os.getenv("VERTEX_MM_EMBED_MODEL", "multimodalembedding@001") or "").strip()
TXT_MODEL = (os.getenv("VERTEX_TXT_EMBED_MODEL", "text-embedding-005") or "").strip()

# ✅ 멀티모달 차원 고정 규칙
# - multimodalembedding@001 은 1408 고정(텍스트/이미지 동일 임베딩 공간)
MM_FIXED_DIM: Optional[int] = 1408 if MM_MODEL == "multimodalembedding@001" else None

# 기대 차원
if MM_FIXED_DIM is not None:
    # 고정 모델이면 env/호출부 실수로 다른 값이 들어오는 걸 원천 차단
    _env_mm_dim = (os.getenv("VERTEX_MM_EMBED_DIM") or "").strip()
    if _env_mm_dim:
        try:
            if int(_env_mm_dim) != MM_FIXED_DIM:
                raise RuntimeError(
                    f"VERTEX_MM_EMBED_MODEL={MM_MODEL} 인데 VERTEX_MM_EMBED_DIM={_env_mm_dim} 입니다. "
                    f"이 모델은 {MM_FIXED_DIM} 고정입니다."
                )
        except ValueError as e:
            raise RuntimeError(f"VERTEX_MM_EMBED_DIM 값이 정수가 아닙니다: {_env_mm_dim}") from e
    MM_DIM = MM_FIXED_DIM
else:
    # 다른 모델이면 기존처럼 env 또는 기본값 사용
    MM_DIM = int(os.getenv("VERTEX_MM_EMBED_DIM", "1408"))

# 텍스트 임베딩 차원 (설정 없으면 None)
TXT_DIM_ENV = os.getenv("VERTEX_TXT_EMBED_DIM")
TXT_DIM = int(TXT_DIM_ENV) if (TXT_DIM_ENV or "").isdigit() else None

# 임베딩 L2 정규화 on/off (기본: 켜짐)
EMBED_L2_NORMALIZE = os.getenv("EMBED_L2_NORMALIZE", "1").lower() not in ("0", "false", "no")

# ⚙️ Vertex 임베딩 요청 크기/길이 제한 (.env 로 조절)
VERTEX_EMBED_MAX_BATCH_DOCS = int(os.getenv("VERTEX_EMBED_MAX_BATCH_DOCS", "16") or 0)
VERTEX_EMBED_MAX_TOTAL_CHARS = int(os.getenv("VERTEX_EMBED_MAX_TOTAL_CHARS", "60000") or 0)
VERTEX_EMBED_MAX_DOC_CHARS = int(os.getenv("VERTEX_EMBED_MAX_DOC_CHARS", "4000") or 0)

# RAG / 표 질의용 LLM 모델 이름 (Vertex GenerativeModel 에서 사용)
GEMINI_RAG_MODEL_ENV = (
    os.getenv("GEMINI_MODEL_RAG")
    or os.getenv("GEMINI_MODEL_DIRECT")
    or None
)

TABLE_LLM_MODEL = (
    os.getenv("GEMINI_MODEL_TABLE")
    or GEMINI_RAG_MODEL_ENV
    or os.getenv("GEMINI_MODEL")
    or os.getenv("GEMINI_TEXT_MODEL")
    or os.getenv("VERTEX_TEXT_MODEL")
    or "gemini-3.5-flash"
)

# =========================================================
# Vertex 공통 초기화
# =========================================================
def _get_vertex_project_id() -> str:
    """
    프로젝트는 VERTEX_PROJECT_ID만 사용한다.
    (혼선 방지를 위해 VERTEX_PROJECT / GOOGLE_CLOUD_PROJECT는 보지 않음)
    """
    project = (os.getenv("VERTEX_PROJECT_ID") or "").strip()
    if not project:
        raise RuntimeError("VERTEX_PROJECT_ID 환경변수가 필요합니다.")
    if project.lower() == "bootstrap":
        raise RuntimeError("VERTEX_PROJECT_ID가 'bootstrap'으로 잡혔습니다. 환경변수를 확인하세요.")
    return project


def _init_vertex(location: str) -> None:
    """
    ✅ 호출 목적에 맞는 Vertex location으로 초기화.
    - 텍스트 임베딩: EMBED_LOCATION
    - 멀티모달 임베딩: MM_LOCATION
    - Gemini LLM: LLM_LOCATION
    """
    if vertexai is None:
        raise RuntimeError(
            "google-cloud-aiplatform (vertexai) 패키지가 필요합니다. "
            "터미널에서 'pip install google-cloud-aiplatform' 후 다시 실행해 주세요."
        )

    project = _get_vertex_project_id()
    vertexai.init(project=project, location=location)
    log.info("Vertex init: project=%s location=%s", project, location)


_mm_model: Optional[Any] = None
_txt_model: Optional[Any] = None
_llm_model: Optional[Any] = None


def _mm() -> Any:
    """멀티모달 임베딩 모델 핸들 (이미지/텍스트 임베딩)."""
    global _mm_model
    _init_vertex(MM_LOCATION)
    if MultiModalEmbeddingModel is None:
        raise RuntimeError(
            "MultiModalEmbeddingModel 클래스를 찾을 수 없습니다. "
            "google-cloud-aiplatform 버전을 확인해 주세요."
        )
    if _mm_model is None:
        _mm_model = MultiModalEmbeddingModel.from_pretrained(MM_MODEL)
    return _mm_model


def _txt() -> Any:
    """텍스트 임베딩 모델 핸들 (text-embedding-005)."""
    global _txt_model
    _init_vertex(EMBED_LOCATION)
    if TextEmbeddingModel is None:
        raise RuntimeError(
            "TextEmbeddingModel 클래스를 찾을 수 없습니다. "
            "google-cloud-aiplatform 버전을 확인해 주세요."
        )
    if _txt_model is None:
        _txt_model = TextEmbeddingModel.from_pretrained(TXT_MODEL)
    return _txt_model


def _llm() -> Any:
    """
    표 질의 해석용 Vertex Gemini LLM 핸들.
    - 서비스 계정 JSON / ADC 로만 동작 (GOOGLE_API_KEY 필요 없음)
    """
    global _llm_model
    _init_vertex(LLM_LOCATION)
    if GenerativeModel is None:
        raise RuntimeError(
            "vertexai.generative_models.GenerativeModel 을 사용할 수 없습니다. "
            "google-cloud-aiplatform 버전 또는 환경을 확인해 주세요."
        )
    if _llm_model is None:
        _llm_model = GenerativeModel(TABLE_LLM_MODEL)
    return _llm_model


# =========================================================
# 유틸 (정규화)
# =========================================================
def _l2_norm(v: List[float]) -> List[float]:
    if not EMBED_L2_NORMALIZE:
        return v
    s = sum(x * x for x in v) ** 0.5
    if s == 0.0:
        return v
    inv = 1.0 / s
    return [x * inv for x in v]


def _l2_norm_many(vs: List[List[float]]) -> List[List[float]]:
    if not EMBED_L2_NORMALIZE:
        return vs
    return [_l2_norm(v) for v in vs]


# =========================================================
# 내부: Vertex 텍스트 임베딩 배치 나누기
# =========================================================
def _iter_vertex_batches(texts: List[str]):
    """
    VERTEX_EMBED_MAX_* 환경변수 기준으로
    - 문서 하나 길이를 잘라주고
    - 요청(batch) 단위로 잘라서 yield.
    """
    max_docs = VERTEX_EMBED_MAX_BATCH_DOCS or 0
    max_total = VERTEX_EMBED_MAX_TOTAL_CHARS or 0
    max_doc = VERTEX_EMBED_MAX_DOC_CHARS or 0

    batch: List[str] = []
    total_chars = 0

    for idx, raw in enumerate(texts):
        t = (raw or "")

        # 문서 하나가 너무 길면 잘라서 사용
        if max_doc > 0 and len(t) > max_doc:
            orig_len = len(t)
            t = t[:max_doc]
            log.debug(
                "vertex_embed: doc %s truncated from %d to %d chars (max_doc)",
                idx,
                orig_len,
                len(t),
            )

        # 배치 전체 길이보다 긴 경우도 잘라줌
        if max_total > 0 and len(t) > max_total:
            orig_len = len(t)
            t = t[:max_total]
            log.debug(
                "vertex_embed: doc %s truncated from %d to %d chars (max_total)",
                idx,
                orig_len,
                len(t),
            )

        need_flush = False
        if max_docs > 0 and len(batch) >= max_docs:
            need_flush = True
        if max_total > 0 and total_chars + len(t) > max_total and batch:
            need_flush = True

        if need_flush:
            yield batch
            batch = []
            total_chars = 0

        batch.append(t)
        total_chars += len(t)

    if batch:
        yield batch


# =========================================================
# 공개 API (임베딩)
# =========================================================
def embed_text_mm(text: str, dim: Optional[int] = None) -> List[float]:
    """
    멀티모달 '텍스트' 임베딩 (이미지 검색용 쿼리 벡터).
    - multimodalembedding@001 은 1408 차원 고정(텍스트/이미지 동일 임베딩 공간)
    """
    if not text or not text.strip():
        raise ValueError("text is empty")

    # 멀티모달 임베딩은 1408 고정. dim을 쓰고 싶다면 1408만 허용.
    if dim is not None and int(dim) != 1408:
        raise ValueError("멀티모달 임베딩은 dimension=1408 고정입니다.")

    # (선택) 문서상 텍스트는 짧게 잘리므로, 길면 앞부분만 써도 됨
    text = " ".join(text.split()[:40])  # 대략 32 tokens 근처로 보수적으로 컷

    try:
        mm = _mm()

        # 최신/권장 시그니처
        try:
            out = mm.get_embeddings(contextual_text=text)
        except TypeError:
            # 구형 호환
            out = mm.get_embeddings(text=text)

        vec = list(getattr(out, "text_embedding", []) or [])
        if not vec:
            raise RuntimeError("빈 벡터가 반환되었습니다.")
        return _l2_norm(vec)

    except Exception as e:
        raise RuntimeError(f"멀티모달 텍스트 임베딩 실패: {e}") from e


def embed_image_file(
    path: str,
    mime: Optional[str] = None,  # mime 는 현재는 로깅/확장용 이지만 시그니처 유지
    dim: Optional[int] = None,
) -> List[float]:
    """
    멀티모달 '이미지' 임베딩 (이미지 자체 벡터).

    ✅ 안전장치:
    - multimodalembedding@001 사용 시 차원은 1408 고정 (텍스트/이미지 동일 공간 보장)
    - dim/MM_DIM이 1408이 아니면 즉시 에러로 차원 혼용을 차단
    """
    model_name = (MM_MODEL or "").strip()
    fixed_dim = 1408 if model_name == "multimodalembedding@001" else None

    # 최종 차원 결정
    if fixed_dim is not None:
        requested = int(dim) if dim is not None else int(MM_DIM)
        if requested != fixed_dim:
            raise ValueError(
                f"멀티모달 모델({model_name})은 dimension={fixed_dim} 고정입니다. (got={requested})"
            )
        d = fixed_dim
    else:
        d = int(dim or MM_DIM)

    try:
        mm = _mm()
        if Image is None:
            raise RuntimeError("vertexai.vision_models.Image 클래스를 찾을 수 없습니다.")

        img = Image.load_from_file(path)

        out = mm.get_embeddings(image=img, dimension=d)

        vec = list(getattr(out, "image_embedding", []) or [])
        if not vec:
            raise RuntimeError("빈 벡터가 반환되었습니다.")
        return _l2_norm(vec)

    except TypeError as e:
        raise RuntimeError(
            "현재 설치된 google-cloud-aiplatform 버전에서 "
            "MultiModalEmbeddingModel.get_embeddings(image=..., dimension=...) "
            "형식을 지원하지 않습니다.\n"
            "터미널에서 'pip install --upgrade google-cloud-aiplatform' 로 "
            "업그레이드한 뒤 다시 시도해 주세요."
        ) from e
    except Exception as e:
        raise RuntimeError(f"이미지 임베딩 실패: {e}") from e


def embed_texts(texts: List[str]) -> List[List[float]]:
    """
    텍스트 전용 임베딩(text-embedding-005).

    - 리스트 입력 → 리스트 출력 (List[List[float]])
    - TXT_DIM 이 설정되어 있으면 output_dimensionality 사용
    - 너무 많은 청크를 한 번에 보내서 Vertex 토큰 한도를 넘지 않도록
      .env 로 조절 가능한 배치 전략을 사용한다.
    """
    if not texts:
        return []

    if local_embeddings_enabled():
        local_dim = TXT_DIM or 384
        if not TXT_DIM:
            log.warning(
                "LOCAL_EMBEDDINGS=1 이지만 VERTEX_TXT_EMBED_DIM 이 설정되지 않아 "
                "기본 %d차원으로 임베딩합니다. 기존에 실제 Vertex(보통 768차원)로 "
                "쌓인 컬렉션과 함께 쓰면 차원 불일치로 검색이 깨질 수 있습니다.",
                local_dim,
            )
        return embed_texts_local(texts, dim=local_dim)

    # .env 에서 토큰/비용 보호용 설정 읽기
    try:
        max_chars_per_item = int(os.getenv("EMBED_MAX_CHARS_PER_ITEM", "8000"))
    except Exception:
        max_chars_per_item = 8000

    try:
        max_batch_size = int(os.getenv("EMBED_MAX_BATCH_SIZE", "8"))
    except Exception:
        max_batch_size = 8

    try:
        max_batch_chars = int(os.getenv("EMBED_MAX_BATCH_CHARS", "16000"))
    except Exception:
        max_batch_chars = 16000

    if max_batch_size <= 0:
        max_batch_size = len(texts)

    # 1) 아이템별 길이 컷
    processed: List[str] = []
    for t in texts:
        s = str(t or "")
        if max_chars_per_item > 0 and len(s) > max_chars_per_item:
            s = s[:max_chars_per_item]
        processed.append(s)

    try:
        model = _txt()
        vecs: List[List[float]] = []

        n = len(processed)
        idx = 0
        while idx < n:
            batch: List[str] = []
            batch_chars = 0

            while idx < n and len(batch) < max_batch_size:
                s = processed[idx]
                s_len = len(s)

                if max_batch_chars > 0 and batch and (batch_chars + s_len) > max_batch_chars:
                    break

                batch.append(s)
                batch_chars += s_len
                idx += 1

            if not batch:
                batch.append(processed[idx])
                idx += 1

            if TXT_DIM:
                embs = model.get_embeddings(batch, output_dimensionality=TXT_DIM)
            else:
                embs = model.get_embeddings(batch)

            vecs.extend([list(e.values) for e in embs])

        return _l2_norm_many(vecs)

    except Exception as e:
        raise RuntimeError(f"텍스트 임베딩 실패(text-embedding-005): {e}") from e


def embed_texts_vertex(texts: List[str]) -> List[List[float]]:
    """표 / CSV 전용 임베딩 헬퍼."""
    return embed_texts(texts)


def current_embed_dim(space: str = "mm") -> int:
    """
    space == 'mm'  -> 멀티모달 차원
    space == 'txt' -> 텍스트 임베딩 차원(환경에 지정 없으면 0)
    """
    if space.lower().startswith("txt"):
        return TXT_DIM or 0
    return MM_DIM


# =========================================================
# 표 질의용 LLM 헬퍼 (Vertex Gemini, 서비스 계정 / ADC 사용)
# =========================================================
def infer_table_query_with_vertex(
    question: str,
    tables: Dict[str, Dict[str, Any]],
    default_table: Optional[str] = None,
) -> Dict[str, Any]:
    """
    자연어 질문 + 여러 개의 표 스키마를 Vertex Gemini 에 넘겨
    {table, filters, group_by, agg, agg_field} JSON 을 돌려주는 함수.
    """
    import json as _json
    import re as _re

    q = (question or "").strip()
    if not q:
        return {}
    if not tables:
        return {}

    # Local development deliberately uses deterministic embeddings and may not
    # have Google credentials or outbound access.  Query parsing is optional;
    # table_machine already has a schema/synonym based local parser, so do not
    # turn every table search into a slow failing Vertex request in this mode.
    if local_embeddings_enabled():
        return {}

    if GenerativeModel is None or vertexai is None:
        log.warning("Vertex LLM(GenerativeModel)이 없어서 infer_table_query_with_vertex 를 건너뜁니다.")
        return {}

    payload = {"tables": tables, "default_table": default_table}

    prompt = (
        "너는 한국어로 된 질문을 표 분석용 구조화 쿼리로 바꿔주는 도우미야.\n"
        "아래 여러 개의 표 스키마와 예시 행들을 보고, 사용자의 질문을 만족하는 조건을 만들어라.\n"
        "반드시 아래 JSON 형식으로만, 다른 설명 없이 한 번만 출력해.\n\n"
        "형식:\n"
        "{\n"
        '  \"table\": \"사용할 테이블 이름(아래 tables 중 하나)\",\n'
        '  \"filters\": [\n'
        '    {\"column\": \"컬럼명\", \"op\": \"=|contains|in\", \"value\": \"값 또는 값 목록\"}\n'
        "  ],\n"
        '  \"group_by\": \"그룹으로 묶을 컬럼명 또는 빈 문자열\",\n'
        '  \"agg\": \"count|sum|avg|min|max 또는 빈 문자열\",\n'
        '  \"agg_field\": \"집계에 사용할 숫자 컬럼명 또는 빈 문자열\"\n'
        "}\n\n"
        "규칙:\n"
        "- filters 에서 op 는 '=', 'contains', 'in' 중 하나만 사용한다.\n"
        "- 어떤 값이 확실하지 않으면 filters 를 비우고, agg / group_by / agg_field 는 빈 문자열로 둔다.\n"
        "- 여러 표 중 어디를 써야 할지 애매하면 default_table 이 있으면 우선 사용하고, 없으면 table 을 빈 문자열로 둔다.\n"
        "- JSON 말고 다른 텍스트는 절대 쓰지 마라.\n\n"
        f"[tables 및 기본 정보]\n{_json.dumps(payload, ensure_ascii=False)}\n\n"
        f"[사용자 질문]\n{q}\n"
    )

    try:
        model = _llm()
        resp = model.generate_content(prompt)

        text = getattr(resp, "text", None)
        if not text and getattr(resp, "candidates", None):
            try:
                parts = resp.candidates[0].content.parts
                text = "".join(getattr(p, "text", "") for p in parts)
            except Exception:
                text = None
        if not text:
            return {}

        m = _re.search(r"\{.*\}", text, _re.S)
        if m:
            text = m.group(0)

        data = _json.loads(text)
        if not isinstance(data, dict):
            return {}

        data.setdefault("table", default_table or "")
        data.setdefault("filters", [])
        data.setdefault("group_by", "")
        data.setdefault("agg", "")
        data.setdefault("agg_field", "")

        table_name = str(data.get("table") or "").strip()
        if table_name and table_name not in tables:
            if default_table and default_table in tables:
                data["table"] = default_table
            else:
                data["table"] = ""
        elif (not table_name) and default_table and default_table in tables:
            data["table"] = default_table

        agg = str(data.get("agg") or "").lower()
        allowed_agg = {"", "count", "sum", "avg", "min", "max"}
        if agg not in allowed_agg:
            data["agg"] = ""

        filters = data.get("filters")
        if not isinstance(filters, list):
            data["filters"] = []
        else:
            data["filters"] = [f for f in filters if isinstance(f, dict)]

        data["group_by"] = str(data.get("group_by") or "")
        data["agg_field"] = str(data.get("agg_field") or "")

        return data

    except Exception as e:
        log.exception("infer_table_query_with_vertex 실패: %s", e)
        return {}
