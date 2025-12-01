# ragapp/services/vertex_embed.py
from __future__ import annotations
import os
import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

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
# 환경 변수
# =========================================================
# 프로젝트/리전
PROJECT = os.getenv("VERTEX_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")

# 임베딩 모델 이름 (.env 만 보고 결정)
MM_MODEL = os.getenv("VERTEX_MM_EMBED_MODEL", "multimodalembedding@001")
TXT_MODEL = os.getenv("VERTEX_TXT_EMBED_MODEL", "text-embedding-004")

# 기대 차원
MM_DIM = int(os.getenv("VERTEX_MM_EMBED_DIM", "1408"))
TXT_DIM_ENV = os.getenv("VERTEX_TXT_EMBED_DIM")
TXT_DIM = int(TXT_DIM_ENV) if (TXT_DIM_ENV or "").isdigit() else None

# 임베딩 L2 정규화 on/off (기본: 켜짐)
EMBED_L2_NORMALIZE = (
    os.getenv("EMBED_L2_NORMALIZE", "1").lower() not in ("0", "false", "no")
)

# ⚙️ Vertex 임베딩 요청 크기/길이 제한 (.env 로 조절)
# - VERTEX_EMBED_MAX_BATCH_DOCS    : 한 번에 보내는 문서 개수 상한
# - VERTEX_EMBED_MAX_TOTAL_CHARS   : 한 요청 안의 전체 문자열 길이 상한
# - VERTEX_EMBED_MAX_DOC_CHARS     : 문서 하나당 최대 길이 (이보다 길면 잘라서 전송)
VERTEX_EMBED_MAX_BATCH_DOCS = int(os.getenv("VERTEX_EMBED_MAX_BATCH_DOCS", "16") or 0)
VERTEX_EMBED_MAX_TOTAL_CHARS = int(os.getenv("VERTEX_EMBED_MAX_TOTAL_CHARS", "60000") or 0)
VERTEX_EMBED_MAX_DOC_CHARS = int(os.getenv("VERTEX_EMBED_MAX_DOC_CHARS", "4000") or 0)

# RAG / 표 질의용 LLM 모델 이름 (Vertex GenerativeModel 에서 사용)
# 우선순위:
#   GEMINI_MODEL_TABLE > GEMINI_MODEL_RAG > GEMINI_MODEL_DIRECT > GEMINI_MODEL > GEMINI_TEXT_MODEL > VERTEX_TEXT_MODEL > 기본값
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
    or "gemini-2.5-flash"
)

# =========================================================
# Vertex 공통 초기화 (임베딩/LLM 공통)
# =========================================================
def _init_once() -> None:
    if vertexai is None:
        raise RuntimeError(
            "google-cloud-aiplatform (vertexai) 패키지가 필요합니다. "
            "터미널에서 'pip install google-cloud-aiplatform' 후 다시 실행해 주세요."
        )
    if not PROJECT:
        raise RuntimeError(
            "VERTEX_PROJECT 또는 GOOGLE_CLOUD_PROJECT 환경변수가 필요합니다."
        )
    if not getattr(_init_once, "_done", False):
        vertexai.init(project=PROJECT, location=LOCATION)
        _init_once._done = True


_mm_model: Optional[Any] = None
_txt_model: Optional[Any] = None
_llm_model: Optional[Any] = None


def _mm() -> Any:
    """멀티모달 임베딩 모델 핸들 (이미지/텍스트 임베딩)."""
    global _mm_model
    _init_once()
    if MultiModalEmbeddingModel is None:
        raise RuntimeError(
            "MultiModalEmbeddingModel 클래스를 찾을 수 없습니다. "
            "google-cloud-aiplatform 버전을 확인해 주세요."
        )
    if _mm_model is None:
        _mm_model = MultiModalEmbeddingModel.from_pretrained(MM_MODEL)
    return _mm_model


def _txt() -> Any:
    """텍스트 임베딩 모델 핸들 (text-embedding-004)."""
    global _txt_model
    _init_once()
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
    _init_once()
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
    - multimodalembedding@001 은 1408 차원 고정(텍스트/이미지 동일 공간) :contentReference[oaicite:2]{index=2}
    """
    if not text or not text.strip():
        raise ValueError("text is empty")

    # 멀티모달 임베딩은 1408 고정. dim을 쓰고 싶다면 1408만 허용.
    if dim is not None and int(dim) != 1408:
        raise ValueError("멀티모달 임베딩은 dimension=1408 고정입니다.")

    # (선택) 문서상 텍스트는 32 tokens 정도로 잘리므로, 길면 앞부분만 써도 됨 :contentReference[oaicite:3]{index=3}
    text = " ".join(text.split()[:40])  # 대충 32 tokens ~ 32 words 근처로 보수적으로 컷

    try:
        mm = _mm()

        # 최신/권장 시그니처
        try:
            out = mm.get_embeddings(contextual_text=text)
        except TypeError:
            # 혹시 모를 구형 호환(환경에 따라 남아있을 수 있음)
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
    """
    d = int(dim or MM_DIM)
    try:
        mm = _mm()
        if Image is None:
            raise RuntimeError(
                "vertexai.vision_models.Image 클래스를 찾을 수 없습니다."
            )
        img = Image.load_from_file(path)
        out = mm.get_embeddings(image=img, dimension=d)
        vec = list(out.image_embedding)
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
    텍스트 전용 임베딩(text-embedding-004).

    - 리스트 입력 → 리스트 출력 (List[List[float]])
    - TXT_DIM 이 설정되어 있으면 output_dimensionality 사용
    - 너무 많은 청크를 한 번에 보내서 Vertex 토큰 한도를 넘지 않도록
      .env 로 조절 가능한 배치 전략을 사용한다.

      EMBED_MAX_CHARS_PER_ITEM : 각 문장(청크) 최대 글자수 (0 이면 제한 없음)
      EMBED_MAX_BATCH_SIZE     : 한 번에 get_embeddings 로 보내는 최대 문장 개수
      EMBED_MAX_BATCH_CHARS    : 한 배치 안에서 총 글자수 상한 (0 이면 제한 없음)
    """
    if not texts:
        return []

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

    # 0 이하 값이면 해당 제한은 사용하지 않음
    if max_batch_size <= 0:
        max_batch_size = len(texts)

    # 1) 아이템별 길이 컷 (너무 긴 텍스트는 잘라서 보냄)
    processed: List[str] = []
    for t in texts:
        s = str(t or "")
        if max_chars_per_item > 0 and len(s) > max_chars_per_item:
            s = s[:max_chars_per_item]
        processed.append(s)

    try:
        model = _txt()
        vecs: List[List[float]] = []

        # 2) 배치 단위로 잘라서 Vertex API 호출
        n = len(processed)
        idx = 0
        while idx < n:
            batch: List[str] = []
            batch_chars = 0

            # 배치 구성
            while idx < n and len(batch) < max_batch_size:
                s = processed[idx]
                s_len = len(s)

                # 총 글자 수 상한 체크 (첫 아이템은 무조건 넣어야 하므로 batch 비어있을 때는 패스)
                if (
                    max_batch_chars > 0
                    and batch
                    and (batch_chars + s_len) > max_batch_chars
                ):
                    break

                batch.append(s)
                batch_chars += s_len
                idx += 1

            if not batch:
                # 안전 장치: 상한이 너무 빡빡해서 아무것도 못 넣는 경우
                batch.append(processed[idx])
                idx += 1

            # Vertex 호출
            if TXT_DIM:
                embs = model.get_embeddings(
                    batch, output_dimensionality=TXT_DIM
                )
            else:
                embs = model.get_embeddings(batch)

            vecs.extend([list(e.values) for e in embs])

        return _l2_norm_many(vecs)

    except Exception as e:
        raise RuntimeError(f"텍스트 임베딩 실패(text-embedding-004): {e}") from e



def embed_texts_vertex(texts: List[str]) -> List[List[float]]:
    """
    표 / CSV 전용 임베딩 헬퍼.
    내부적으로 embed_texts(...) 와 동일한 Vertex Text Embedding 설정을 사용합니다.
    """
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
# 표 질의용 LLM 헬퍼 (Vertex Gemini, 서비스 계정 JSON 사용)
# =========================================================
def infer_table_query_with_vertex(
    question: str,
    tables: Dict[str, Dict[str, Any]],
    default_table: Optional[str] = None,
) -> Dict[str, Any]:
    """
    자연어 질문 + 여러 개의 표 스키마를 Vertex Gemini 에 넘겨
    {table, filters, group_by, agg, agg_field} JSON 을 돌려주는 함수.

    feature_views.table_search_view 에서 기대하는 형식:
      {
        "table": "선택된 테이블 이름",
        "filters": [
          {"column": "region", "op": "=", "value": "서울"},
          {"column": "product", "op": "in", "value": ["아메리카노", "라떼"]}
        ],
        "group_by": "region",
        "agg": "sum",
        "agg_field": "sales"
      }

    - question : 사용자의 자연어 질문
    - tables   : {
        "매출표": {
          "columns": [...],
          "column_types": {...},
          "sample_rows": [{...}, ...]
        },
        ...
      }
    - default_table : 폼에서 사용자가 선택한 테이블(없을 수 있음)

    ⚠️ LLM 을 전혀 쓰고 싶지 않으면, 이 함수를 호출하는 쪽(feature_views)에서
       infer_table_query_with_vertex 가 None 이거나 {} 를 리턴하면 그냥 무시된다.
    """
    import json as _json
    import re as _re

    q = (question or "").strip()
    if not q:
        return {}
    if not tables:
        return {}

    # LLM 기능이 아예 없는 환경이면 바로 포기 (임베딩 + JSON fallback만 사용)
    if GenerativeModel is None or vertexai is None:
        log.warning(
            "Vertex LLM(GenerativeModel)이 없어서 infer_table_query_with_vertex 를 건너뜁니다."
        )
        return {}

    payload = {
        "tables": tables,
        "default_table": default_table,
    }

    # 프롬프트: 무조건 JSON 하나만, 조건/집계까지 정리해 달라고 요청
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
        "- 예) \"서울 지역 매출만\" → column:\"region\", op:\"=\", value:\"Seoul\".\n"
        "- 예) \"서울이랑 부산 합쳐서\" → column:\"region\", op:\"in\", value:[\"Seoul\",\"Busan\"].\n"
        "- 어떤 값이 확실하지 않으면 filters 를 비우고, agg / group_by / agg_field 는 빈 문자열로 둔다.\n"
        "- 여러 표 중 어디를 써야 할지 애매하면 default_table 이 있으면 우선 사용하고, 없으면 table 을 빈 문자열로 둔다.\n"
        "- JSON 말고 다른 텍스트는 절대 쓰지 마라.\n\n"
        f"[tables 및 기본 정보]\n{_json.dumps(payload, ensure_ascii=False)}\n\n"
        f"[사용자 질문]\n{q}\n"
    )

    try:
        model = _llm()
        resp = model.generate_content(prompt)

        # SDK 버전에 따라 resp.text 또는 candidates[0].content.parts 로 나올 수 있음
        text = getattr(resp, "text", None)
        if not text and getattr(resp, "candidates", None):
            try:
                parts = resp.candidates[0].content.parts
                text = "".join(getattr(p, "text", "") for p in parts)
            except Exception:
                text = None
        if not text:
            return {}

        # 응답에서 JSON 부분만 추출
        m = _re.search(r"\{.*\}", text, _re.S)
        if m:
            text = m.group(0)

        data = _json.loads(text)
        if not isinstance(data, dict):
            return {}

        # 기본 필드 채우기
        data.setdefault("table", default_table or "")
        data.setdefault("filters", [])
        data.setdefault("group_by", "")
        data.setdefault("agg", "")
        data.setdefault("agg_field", "")

        # table 정합성 체크
        table_name = str(data.get("table") or "").strip()
        if table_name and table_name not in tables:
            # LLM 이 이상한 이름을 준 경우 → default_table 이 유효하면 그걸로, 아니면 공백
            if default_table and default_table in tables:
                data["table"] = default_table
            else:
                data["table"] = ""
        elif (not table_name) and default_table and default_table in tables:
            data["table"] = default_table

        # agg 정리
        agg = str(data.get("agg") or "").lower()
        allowed_agg = {"", "count", "sum", "avg", "min", "max"}
        if agg not in allowed_agg:
            data["agg"] = ""

        # filters 는 리스트만 허용
        filters = data.get("filters")
        if not isinstance(filters, list):
            data["filters"] = []
        else:
            # dict 아닌 항목 제거
            data["filters"] = [f for f in filters if isinstance(f, dict)]

        # 문자열 필드 보정
        data["group_by"] = str(data.get("group_by") or "")
        data["agg_field"] = str(data.get("agg_field") or "")

        return data
    except Exception as e:
        # 여기서 예외가 나도, 표 검색 전체가 죽지 않고 그냥 LLM 보조만 끄는 쪽으로
        log.exception("infer_table_query_with_vertex 실패: %s", e)
        return {}
