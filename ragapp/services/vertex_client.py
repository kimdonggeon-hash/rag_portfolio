# ragapp/services/vertex_client.py

from __future__ import annotations
import os
from functools import lru_cache

from google import genai
from google.genai.types import HttpOptions

# ✅ 프로젝트 / 리전은 기존 .env 그대로 사용
PROJECT = os.environ.get("VERTEX_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")


@lru_cache(maxsize=1)
def get_vertex_client() -> genai.Client:
    """
    Vertex AI Gemini 클라이언트 (서비스 계정 JSON / ADC 전용 버전).

    - GOOGLE_API_KEY 필요 없음
    - GOOGLE_APPLICATION_CREDENTIALS 로 서비스 계정 JSON 지정
    - VERTEX_PROJECT / VERTEX_LOCATION 으로 Vertex 프로젝트/리전 선택
    """
    if not PROJECT:
        raise RuntimeError(
            "VERTEX_PROJECT 또는 GOOGLE_CLOUD_PROJECT 환경변수가 필요합니다."
        )

    # 🔹 vertexai=True + project/location 조합 → ADC(서비스 계정)로 Vertex 엔드포인트 사용
    return genai.Client(
        vertexai=True,
        project=PROJECT,
        location=LOCATION,
        http_options=HttpOptions(api_version="v1"),
    )
