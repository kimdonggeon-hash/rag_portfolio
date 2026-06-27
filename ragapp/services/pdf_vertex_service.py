# ragapp/services/pdf_vertex_service.py
from __future__ import annotations

import json
import logging
import os
from typing import Literal, Optional

from django.conf import settings

log = logging.getLogger(__name__)

try:
    import vertexai
    from vertexai.generative_models import GenerativeModel, GenerationConfig
except Exception:  # vertexai 미설치 대비
    vertexai = None
    GenerativeModel = None  # type: ignore[assignment]
    GenerationConfig = None  # type: ignore[assignment]

try:
    from google.oauth2 import service_account
except Exception:  # pragma: no cover
    service_account = None  # type: ignore[assignment]


class VertexNotConfiguredError(RuntimeError):
    """Vertex 설정/라이브러리 문제용 예외"""


class VertexEmptyOutputError(RuntimeError):
    """Vertex가 텍스트(parts) 없이 빈 응답을 반환한 경우"""


_vertex_initialized = False


def _first_non_empty(*values: object) -> Optional[str]:
    """
    None / 빈 문자열을 제외하고 가장 먼저 들어온 값을 문자열로 반환.
    """
    for value in values:
        if value is None:
            continue

        text = str(value).strip()
        if text:
            return text

    return None


def _load_credentials_and_project() -> tuple[Optional[object], Optional[str], str]:
    """
    서비스 계정 JSON / 파일 경로 / 프로젝트 ID를 다양하게 지원.

    우선순위:
      1) .env에 JSON 문자열 (VERTEX_SERVICE_ACCOUNT_JSON / VERTEX_JSON_KEY / VERTEX_JSON)
      2) GOOGLE_APPLICATION_CREDENTIALS = JSON 파일 경로
      3) 별도 자격 증명 없이 기본 환경 (Cloud Run 기본 서비스 계정 등)

    중요:
      - location은 VERTEX_LOCATION을 최우선으로 사용.
      - Cloud Run에서 VERTEX_LOCATION=global 로 지정하면 PDF Vertex 호출도 global을 사용.
    """
    project_id = _first_non_empty(
        os.getenv("VERTEX_PROJECT_ID"),
        os.getenv("VERTEX_PROJECT"),
        os.getenv("GOOGLE_CLOUD_PROJECT"),
        os.getenv("GCP_PROJECT"),
        getattr(settings, "VERTEX_PROJECT_ID", None),
        getattr(settings, "VERTEX_PROJECT", None),
        getattr(settings, "GOOGLE_CLOUD_PROJECT", None),
    )

    location = _first_non_empty(
        # ✅ 가장 중요: Cloud Run에서 넣은 VERTEX_LOCATION을 최우선 사용
        os.getenv("VERTEX_LOCATION"),
        os.getenv("GOOGLE_CLOUD_LOCATION"),

        # PDF 전용 location이 있으면 그 다음 순위
        os.getenv("PDF_VERTEX_LOCATION"),
        os.getenv("PDF_GEMINI_LOCATION"),
        os.getenv("VERTEX_LLM_LOCATION"),

        # settings.py 값은 env보다 후순위
        getattr(settings, "VERTEX_LOCATION", None),
        getattr(settings, "GOOGLE_CLOUD_LOCATION", None),
        getattr(settings, "PDF_VERTEX_LOCATION", None),
        getattr(settings, "PDF_GEMINI_LOCATION", None),
        getattr(settings, "VERTEX_LLM_LOCATION", None),

        # 기본값
        "global",
    ) or "global"

    if service_account is None:
        return None, project_id, location

    # 1) 환경 변수에 JSON 문자열로 넣어둔 경우
    json_str = _first_non_empty(
        os.getenv("VERTEX_SERVICE_ACCOUNT_JSON"),
        os.getenv("VERTEX_JSON_KEY"),
        os.getenv("VERTEX_JSON"),
    )

    if json_str:
        info = json.loads(json_str)
        creds = service_account.Credentials.from_service_account_info(info)
        project_id = project_id or info.get("project_id")
        return creds, project_id, location

    # 2) GOOGLE_APPLICATION_CREDENTIALS = JSON 파일 경로
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

    if cred_path and os.path.exists(cred_path):
        with open(cred_path, "r", encoding="utf-8") as f:
            info = json.load(f)

        creds = service_account.Credentials.from_service_account_file(cred_path)
        project_id = project_id or info.get("project_id")
        return creds, project_id, location

    # 3) 서비스 계정 정보를 못 찾은 경우 → Cloud Run 기본 서비스 계정 / 기본 인증 사용
    return None, project_id, location


def _ensure_vertex() -> None:
    """vertexai.init 1번만 호출 + JSON 기반 자격 증명 지원."""
    global _vertex_initialized

    if vertexai is None or GenerativeModel is None or GenerationConfig is None:
        raise VertexNotConfiguredError(
            "vertexai 라이브러리가 없습니다. "
            "터미널에서 'pip install google-cloud-aiplatform' 실행 후 서버를 다시 켜 주세요."
        )

    if _vertex_initialized:
        return

    creds, project_id, location = _load_credentials_and_project()

    log.warning(
        "[PDF Vertex init] project=%s, location=%s",
        project_id,
        location,
    )

    if not project_id:
        raise VertexNotConfiguredError(
            "Vertex 프로젝트 ID를 찾지 못했습니다. "
            "환경변수 VERTEX_PROJECT_ID, VERTEX_PROJECT, GOOGLE_CLOUD_PROJECT "
            "또는 서비스 계정 JSON 안의 project_id를 확인해 주세요."
        )

    if creds is not None:
        vertexai.init(project=project_id, location=location, credentials=creds)
    else:
        vertexai.init(project=project_id, location=location)

    _vertex_initialized = True

    log.info(
        "Vertex AI 초기화 완료 (project=%s, location=%s)",
        project_id,
        location,
    )


AnswerMode = Literal["summary", "table"]


def _load_pdf_model_name() -> str:
    """
    PDF 요약/질의응답에 사용할 Gemini 모델명 로드.

    우선순위:
      1) GEMINI_MODEL_PDF
      2) PDF_GEMINI_MODEL
      3) GEMINI_MODEL_RAG
      4) GEMINI_MODEL
      5) VERTEX_TEXT_MODEL
      6) settings.py 값
      7) 기본값 gemini-3.5-flash
    """
    model_name = _first_non_empty(
        os.getenv("GEMINI_MODEL_PDF"),
        os.getenv("PDF_GEMINI_MODEL"),
        os.getenv("GEMINI_MODEL_RAG"),
        os.getenv("GEMINI_MODEL"),
        os.getenv("VERTEX_TEXT_MODEL"),
        getattr(settings, "GEMINI_MODEL_PDF", None),
        getattr(settings, "PDF_GEMINI_MODEL", None),
        getattr(settings, "GEMINI_MODEL_RAG", None),
        getattr(settings, "GEMINI_MODEL", None),
        getattr(settings, "VERTEX_TEXT_MODEL", None),
        "gemini-3.5-flash",
    )

    return model_name or "gemini-3.5-flash"


def _safe_resp_text(resp) -> Optional[str]:
    """
    resp.text는 프로퍼티라서 예외를 던질 수 있음.
    parts가 비어있거나 safety로 막히면 여기서 터지는 케이스가 있음.
    """
    try:
        t = resp.text  # type: ignore[attr-defined]
        return t if t else None
    except Exception as e:
        log.warning("Vertex resp.text 추출 실패(무시하고 candidates로 fallback): %s", e)
        return None


def _collect_candidate_text(resp) -> str:
    """
    candidates[0].content.parts[].text를 모아서 하나의 문자열로 합친다.
    첫 후보만 사용.
    """
    cands = getattr(resp, "candidates", None) or []

    if not cands:
        return ""

    cand = cands[0]
    content = getattr(cand, "content", None)
    parts = getattr(content, "parts", None) or []

    chunks: list[str] = []

    for p in parts:
        t = getattr(p, "text", None)
        if t:
            chunks.append(t)

    return "\n".join(chunks).strip()


def _first_finish_reason(resp) -> str:
    try:
        cands = getattr(resp, "candidates", None) or []

        if not cands:
            return ""

        fr = getattr(cands[0], "finish_reason", "")
        return str(fr) if fr is not None else ""

    except Exception:
        return ""


def summarize_pdf_text_with_vertex(
    text: str,
    question: str | None = None,
    *,
    mode: AnswerMode = "summary",
    max_chars: int = 16000,
) -> str:
    """
    PDF에서 추출한 텍스트 + 사용자의 요청을 Vertex(Gemini)로 보내서
    요약 또는 표(마크다운)로 정리된 답변을 반환.

    - mode="summary" → 일반 요약
    - mode="table"   → 마크다운 표 중심으로 정리
    """
    _ensure_vertex()

    if not text:
        raise ValueError("PDF 텍스트가 비어 있습니다.")

    # 너무 긴 문서는 앞부분만 사용
    if len(text) > max_chars:
        text = text[:max_chars]

    model_name = _load_pdf_model_name()

    log.warning("[PDF Vertex model] model=%s", model_name)

    model = GenerativeModel(model_name)

    base_instruction = (
        "너는 한국어 PDF 문서를 요약해 주는 도우미야. "
        "사용자의 요청에 맞춰 가능한 한 간결하게 정리해 줘."
    )

    if mode == "table":
        style_instruction = (
            "출력은 반드시 **마크다운 표** 형식으로 만들어 줘. "
            "표 위에 아주 짧은 한 줄 제목만 쓰고, 나머지는 모두 마크다운 표로 정리해. "
            "표의 헤더는 명확한 한국어로 작성해."
        )
    else:
        style_instruction = (
            "출력은 한국어로 5~10줄 정도의 문단 요약으로 작성해. "
            "불필요한 서론은 빼고 핵심만 정리해."
        )

    user_request = question.strip() if question else "이 문서의 핵심 내용을 정리해줘."

    prompt = (
        f"{base_instruction}\n{style_instruction}\n\n"
        "아래는 PDF에서 추출한 원문 텍스트야.\n\n"
        "----- PDF 내용 시작 -----\n"
        f"{text}\n"
        "----- PDF 내용 끝 -----\n\n"
        f"사용자 요청: {user_request}\n"
    )

    gen_config = GenerationConfig(
        temperature=0.3 if mode == "table" else 0.5,
        max_output_tokens=2048,
    )

    resp = model.generate_content(prompt, generation_config=gen_config)

    # 1) resp.text는 예외가 날 수 있으니 안전하게 추출
    answer = _safe_resp_text(resp)

    # 2) 없으면 candidates에서 텍스트 모으기
    if not answer:
        answer2 = _collect_candidate_text(resp)
        answer = answer2 if answer2 else None

    # 3) 그래도 비면: 빈 응답(차단/빈 parts/토큰종료 등)
    if not answer:
        fr = _first_finish_reason(resp)
        usage = getattr(resp, "usage_metadata", None)

        log.warning(
            "Vertex 빈 응답 반환 (finish_reason=%s, usage=%s, model=%s)",
            fr,
            usage,
            model_name,
        )

        raise VertexEmptyOutputError(f"empty output (finish_reason={fr})")

    return answer.strip()