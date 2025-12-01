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
    GenerativeModel = None

try:
    from google.oauth2 import service_account
except Exception:  # pragma: no cover
    service_account = None  # type: ignore[assignment]


class VertexNotConfiguredError(RuntimeError):
    """Vertex 설정/라이브러리 문제용 예외"""


_vertex_initialized = False


def _load_credentials_and_project() -> tuple[Optional[object], Optional[str], str]:
    """
    서비스 계정 JSON / 파일 경로 / 프로젝트 ID를 다양하게 지원.

    우선순위:
      1) .env에 JSON 문자열 (VERTEX_SERVICE_ACCOUNT_JSON / VERTEX_JSON_KEY / VERTEX_JSON)
      2) GOOGLE_APPLICATION_CREDENTIALS = JSON 파일 경로
      3) 별도 자격 증명 없이 기본 환경 (gcloud auth 등)
    """
    if service_account is None:
        project_id = getattr(settings, "VERTEX_PROJECT_ID", None) or os.getenv("VERTEX_PROJECT_ID")
        location = getattr(settings, "VERTEX_LOCATION", None) or os.getenv("VERTEX_LOCATION") or "asia-northeast3"
        return None, project_id, location

    project_id = getattr(settings, "VERTEX_PROJECT_ID", None) or os.getenv("VERTEX_PROJECT_ID")
    location = getattr(settings, "VERTEX_LOCATION", None) or os.getenv("VERTEX_LOCATION") or "asia-northeast3"

    # 1) 환경 변수에 JSON 문자열로 넣어둔 경우
    json_str = (
        os.getenv("VERTEX_SERVICE_ACCOUNT_JSON")
        or os.getenv("VERTEX_JSON_KEY")
        or os.getenv("VERTEX_JSON")
    )
    if json_str:
        info = json.loads(json_str)
        creds = service_account.Credentials.from_service_account_info(info)
        project_id = project_id or info.get("project_id")
        return creds, project_id, location

    # 2) GOOGLE_APPLICATION_CREDENTIALS = 파일 경로
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if cred_path and os.path.exists(cred_path):
        with open(cred_path, "r", encoding="utf-8") as f:
            info = json.load(f)
        creds = service_account.Credentials.from_service_account_file(cred_path)
        project_id = project_id or info.get("project_id")
        return creds, project_id, location

    # 3) 서비스 계정 정보를 못 찾은 경우 → project_id만 settings/env에서 사용
    return None, project_id, location


def _ensure_vertex() -> None:
    """vertexai.init 1번만 호출 + JSON 기반 자격 증명 지원."""
    global _vertex_initialized

    if vertexai is None or GenerativeModel is None:
        raise VertexNotConfiguredError(
            "vertexai 라이브러리가 없습니다. "
            "터미널에서 'pip install google-cloud-aiplatform' 실행 후 서버를 다시 켜 주세요."
        )

    if _vertex_initialized:
        return

    creds, project_id, location = _load_credentials_and_project()

    if not project_id:
        raise VertexNotConfiguredError(
            "Vertex 프로젝트 ID를 찾지 못했습니다. "
            "settings.VERTEX_PROJECT_ID 또는 환경변수 VERTEX_PROJECT_ID, "
            "또는 서비스 계정 JSON 안의 project_id를 확인해 주세요."
        )

    if creds is not None:
        vertexai.init(project=project_id, location=location, credentials=creds)
    else:
        vertexai.init(project=project_id, location=location)

    _vertex_initialized = True
    log.info("Vertex AI 초기화 완료 (project=%s, location=%s)", project_id, location)


AnswerMode = Literal["summary", "table"]


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

    # 너무 긴 문서는 앞부분만 사용 (간단 보호장치)
    if len(text) > max_chars:
        text = text[:max_chars]

    model_name = (
        getattr(settings, "VERTEX_TEXT_MODEL", None)
        or os.getenv("VERTEX_TEXT_MODEL")
        or "gemini-1.5-pro"
    )
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
        max_output_tokens=1024,
    )

    resp = model.generate_content(prompt, generation_config=gen_config)

    # 최신 vertexai SDK 기준 .text 가 있으면 그걸 우선, 없으면 candidates에서 모으기
    answer = getattr(resp, "text", None)
    if not answer and getattr(resp, "candidates", None):
        chunks: list[str] = []
        for cand in resp.candidates:
            parts = getattr(getattr(cand, "content", None), "parts", []) or []
            for p in parts:
                t = getattr(p, "text", None)
                if t:
                    chunks.append(t)
        answer = "\n".join(chunks)

    return (answer or "").strip()
