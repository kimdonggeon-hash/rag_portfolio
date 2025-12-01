# ragapp/news_views/pdf_views.py
from __future__ import annotations

import io
import logging

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from ragapp.services.pdf_vertex_service import (
    summarize_pdf_text_with_vertex,
    VertexNotConfiguredError,
)

log = logging.getLogger(__name__)

# pypdf로 PDF 텍스트 추출
try:
    from pypdf import PdfReader  # pip install pypdf
except Exception:
    PdfReader = None  # type: ignore[assignment]

# 마크다운 → HTML 변환용 (표 렌더링)
try:
    import markdown  # type: ignore
except Exception:
    markdown = None  # type: ignore[assignment]


def _extract_text_from_pdf(uploaded_file) -> str:
    """
    업로드된 PDF에서 텍스트를 쭉 뽑아 하나의 문자열로 합친다.
    pypdf가 없으면 RuntimeError를 던진다.
    """
    if PdfReader is None:
        raise RuntimeError(
            "PDF 텍스트 추출 라이브러리(pypdf)가 설치되어 있지 않습니다. "
            "터미널에서 'pip install pypdf' 를 실행한 뒤 서버를 다시 켜 주세요."
        )

    data = uploaded_file.read()
    reader = PdfReader(io.BytesIO(data))

    texts: list[str] = []
    for page in reader.pages:
        try:
            texts.append(page.extract_text() or "")
        except Exception as e:
            log.warning("PDF 페이지 텍스트 추출 실패: %s", e)

    return "\n\n".join(texts).strip()


@csrf_protect
@require_POST
def pdf_analyze_api_view(request: HttpRequest) -> JsonResponse:
    """
    PDF 업로드 + Vertex(Gemini) 요약/표 정리 API.

    - 입력: 파일(pdf), question(선택), mode = summary / table
    - 출력: { ok, answer_text, answer_html, mode }
      * answer_text : 원문 마크다운/텍스트
      * answer_html : 마크다운을 HTML로 변환한 결과(표 포함) – JS에서 우선 사용
    """
    pdf_file = request.FILES.get("pdf")
    question = (request.POST.get("question") or "").strip()
    mode = (request.POST.get("mode") or "summary").strip()
    if mode not in ("summary", "table"):
        mode = "summary"

    if not pdf_file:
        return JsonResponse(
            {"ok": False, "error": "PDF 파일을 하나 선택해 주세요."},
            status=400,
        )

    file_name = getattr(pdf_file, "name", "(이름 없음)")

    # 1) PDF에서 텍스트 추출
    try:
        text = _extract_text_from_pdf(pdf_file)
        if not text:
            return JsonResponse(
                {
                    "ok": False,
                    "error": "PDF에서 텍스트를 거의 찾지 못했습니다. "
                             "이미지 형태의 PDF라면 OCR이 필요할 수 있습니다.",
                },
                status=400,
            )
    except Exception as e:
        log.exception("PDF 텍스트 추출 중 오류")
        return JsonResponse(
            {
                "ok": False,
                "error": f"PDF 텍스트 추출 중 오류가 발생했습니다: {e}",
            },
            status=500,
        )

    # 2) Vertex(Gemini)로 요약/표 정리
    try:
        answer = summarize_pdf_text_with_vertex(text, question, mode=mode)  # type: ignore[arg-type]
    except VertexNotConfiguredError as e:
        return JsonResponse(
            {"ok": False, "error": f"Vertex 설정 오류: {e}"},
            status=500,
        )
    except Exception as e:
        log.exception("Vertex PDF 분석 중 오류")
        return JsonResponse(
            {"ok": False, "error": f"Vertex AI 호출 중 오류가 발생했습니다: {e}"},
            status=500,
        )

    if not answer:
        answer = "Vertex AI로부터 응답을 받지 못했습니다. 설정을 다시 확인해 주세요."

    # 3) 개발 중 디버그 정보는 응답 본문이 아니라 로그로만 남김
    if getattr(settings, "DEBUG", False):
        log.info(
            "PDF 분석 디버그: file=%s mode=%s question=%s text_len=%d",
            file_name,
            mode,
            question,
            len(text),
        )

    # 4) Vertex가 준 마크다운/텍스트를 HTML로 변환 (표 렌더링용)
    answer_main = answer
    answer_html: str | None = None

    if markdown is not None:
        try:
            answer_html = markdown.markdown(
                answer_main,
                extensions=["tables", "fenced_code", "nl2br"],
                output_format="html5",
            )
        except Exception as e:
            log.warning("PDF markdown 변환 실패: %s", e)
            answer_html = None

    return JsonResponse(
        {
            "ok": True,
            "answer_text": answer_main,  # fallback용 원문
            "answer_html": answer_html,  # 있으면 JS에서 innerHTML로 사용
            "mode": mode,
        }
    )
