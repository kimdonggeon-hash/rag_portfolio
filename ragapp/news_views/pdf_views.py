# ragapp/news_views/pdf_views.py
from __future__ import annotations

import io
import logging
from typing import List, Tuple

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from ragapp.services.usage_limiter import check_and_increment_usage
from ragapp.services.pdf_vertex_service import (
    summarize_pdf_text_with_vertex,
    VertexNotConfiguredError,
)

log = logging.getLogger(__name__)

# markdown -> html (표 렌더)
try:
    import markdown  # type: ignore
except Exception:
    markdown = None  # type: ignore[assignment]


def _is_staff_request(request: HttpRequest) -> bool:
    """
    ✅ 스태프/슈퍼유저로 로그인된 요청인지 판단.
    """
    try:
        u = getattr(request, "user", None)
        return bool(
            u
            and getattr(u, "is_authenticated", False)
            and (getattr(u, "is_staff", False) or getattr(u, "is_superuser", False))
        )
    except Exception:
        return False


def _get_limits(*, is_staff: bool) -> tuple[int, int, int]:
    """
    settings.py 값 사용 (없으면 안전 기본값)

    일반 유저:
      - PDF_ANALYZE_MAX_FILES
      - PDF_ANALYZE_MAX_SIZE_MB
      - PDF_ANALYZE_MAX_TOTAL_CHARS

    스태프:
      - 사실상 제한 해제(큰 값) + Cloud Run 안전을 위한 절대 상한만 유지
    """
    max_files = int(getattr(settings, "PDF_ANALYZE_MAX_FILES", 3) or 3)
    max_size_mb = int(getattr(settings, "PDF_ANALYZE_MAX_SIZE_MB", 10) or 10)
    max_total_chars = int(getattr(settings, "PDF_ANALYZE_MAX_TOTAL_CHARS", 60000) or 60000)

    if not is_staff:
        return max_files, max_size_mb, max_total_chars

    staff_abs_max_files = int(getattr(settings, "PDF_ANALYZE_STAFF_ABS_MAX_FILES", 30) or 30)
    staff_abs_max_size_mb = int(getattr(settings, "PDF_ANALYZE_STAFF_ABS_MAX_SIZE_MB", 200) or 200)
    staff_abs_max_total_chars = int(
        getattr(settings, "PDF_ANALYZE_STAFF_ABS_MAX_TOTAL_CHARS", 2_000_000) or 2_000_000
    )
    return staff_abs_max_files, staff_abs_max_size_mb, staff_abs_max_total_chars


def _read_uploaded_bytes(f) -> bytes:
    try:
        f.seek(0)
    except Exception:
        pass
    data = f.read() or b""
    try:
        f.seek(0)
    except Exception:
        pass
    return data


def _pdf_to_text(pdf_bytes: bytes) -> str:
    """
    1) pdfminer.six 우선
    2) 실패 시 pypdf 폴백
    """
    try:
        from pdfminer.high_level import extract_text  # type: ignore
        return (extract_text(io.BytesIO(pdf_bytes)) or "").strip()
    except Exception as e1:
        try:
            from pypdf import PdfReader  # type: ignore
            reader = PdfReader(io.BytesIO(pdf_bytes))
            parts: List[str] = []
            for p in reader.pages:
                try:
                    parts.append(p.extract_text() or "")
                except Exception:
                    continue
            return ("\n\n".join(parts)).strip()
        except Exception as e2:
            raise RuntimeError(f"PDF 텍스트 추출 실패(pdfminer/pypdf 필요): {e1 or e2}") from e2


def _md_to_html(md_text: str) -> str | None:
    if markdown is None:
        return None
    try:
        return markdown.markdown(
            md_text,
            extensions=["tables", "fenced_code", "nl2br"],
            output_format="html5",
        )
    except Exception as e:
        log.warning("PDF markdown 변환 실패: %s", e)
        return None


@csrf_protect
@require_POST
def pdf_analyze_api_view(request: HttpRequest) -> JsonResponse:
    """
    PDF 업로드 + Vertex(Gemini) 요약/표 정리 API.

    ✅ 스태프/슈퍼유저: 파일/용량/텍스트 제한을 큰 상한으로 완화
    ✅ 일반 사용자: settings.py 제한 적용
    ✅ 하루 사용량(pdf)은 "실제로 분석 가능한 텍스트가 확보된 뒤" + "Vertex 호출 직전"에만 차감
       (실패 요청으로 카운트 깎이는 문제 방지)

    입력:
      - files: pdffiles / pdfs / pdf 모두 허용
      - question (선택)
      - mode: summary / table

    출력:
      - { ok, answer_text, answer_html, mode, file_errors?, files_used? }
    """
    is_staff = _is_staff_request(request)

    mode = (request.POST.get("mode") or "summary").strip()
    if mode not in ("summary", "table"):
        mode = "summary"
    question = (request.POST.get("question") or "").strip()

    # ---- 파일 수집(키 호환) ----
    files = []
    if hasattr(request, "FILES"):
        files = (
            request.FILES.getlist("pdffiles")
            or request.FILES.getlist("pdfs")
            or request.FILES.getlist("pdf")
            or []
        )
        if not files and request.FILES.get("pdf"):
            files = [request.FILES["pdf"]]

    if not files:
        return JsonResponse({"ok": False, "error": "PDF 파일을 하나 이상 선택해 주세요."}, status=400)

    max_files, max_size_mb, max_total_chars = _get_limits(is_staff=is_staff)
    max_size_bytes = max_size_mb * 1024 * 1024 if max_size_mb > 0 else 0

    # ---- 제한: 파일 개수 ----
    if max_files > 0 and len(files) > max_files:
        return JsonResponse(
            {
                "ok": False,
                "error": f"한 번에 분석할 수 있는 PDF는 최대 {max_files}개까지입니다."
                + (" (스태프 상한)" if is_staff else ""),
            },
            status=400,
        )

    # ---- 파일별 검증 + 텍스트 추출 ----
    file_errors: List[str] = []
    extracted_blocks: List[Tuple[str, str]] = []
    total_chars = 0

    for f in files:
        fname = getattr(f, "name", "(이름 없음)")
        lname = str(fname).lower()

        if not lname.endswith(".pdf"):
            file_errors.append(f"{fname}: PDF 파일만 분석할 수 있어요.")
            continue

        fsize = int(getattr(f, "size", 0) or 0)
        if max_size_bytes and fsize > max_size_bytes:
            file_errors.append(
                f"{fname}: 파일 크기가 {max_size_mb}MB를 초과했습니다." + (" (스태프 상한)" if is_staff else "")
            )
            continue

        try:
            pdf_bytes = _read_uploaded_bytes(f)
            text = _pdf_to_text(pdf_bytes).strip()
            if not text:
                file_errors.append(
                    f"{fname}: 텍스트를 거의 찾지 못했습니다. (이미지 PDF면 OCR이 필요할 수 있어요.)"
                )
                continue

            # ---- 총 텍스트 길이 제한 ----
            if max_total_chars > 0:
                remain = max_total_chars - total_chars
                if remain <= 0:
                    break
                if len(text) > remain:
                    text = text[:remain]
                total_chars += len(text)

            extracted_blocks.append((fname, text))

        except Exception as e:
            log.exception("PDF 텍스트 추출 실패: %s", fname)
            file_errors.append(f"{fname}: 텍스트 추출 오류 - {e}")

    if not extracted_blocks:
        return JsonResponse(
            {"ok": False, "error": "PDF 텍스트를 추출하지 못했습니다.", "file_errors": file_errors},
            status=400,
        )

    # ---- Vertex 입력 텍스트 구성(파일명 헤더 포함) ----
    combined_text = "\n\n".join([f"[FILE] {fn}\n{tx}" for fn, tx in extracted_blocks]).strip()

    # ✅ (중요) 여기서부터 '정상 분석 시도'로 보고 일일 제한(pdf) 차감
    allowed, limit, used_after = check_and_increment_usage(request, "pdf")
    if not allowed:
        return JsonResponse(
            {
                "ok": False,
                "error": "DAILY_LIMIT_REACHED",
                "kind": "pdf",
                "limit": limit,
                "used": used_after,
                "message": f"오늘 pdf 분석은 {limit}회까지 가능합니다.",
            },
            status=429,
            json_dumps_params={"ensure_ascii": False},
        )

    # ---- Vertex 호출 ----
    try:
        answer = summarize_pdf_text_with_vertex(combined_text, question, mode=mode)  # type: ignore[arg-type]
    except VertexNotConfiguredError as e:
        return JsonResponse(
            {"ok": False, "error": f"Vertex 설정 오류: {e}", "file_errors": file_errors},
            status=500,
        )
    except Exception as e:
        log.exception("Vertex PDF 분석 중 오류")
        return JsonResponse(
            {"ok": False, "error": f"Vertex AI 호출 중 오류가 발생했습니다: {e}", "file_errors": file_errors},
            status=500,
        )

    answer_main = (answer or "").strip() or "Vertex AI로부터 응답을 받지 못했습니다. 설정을 다시 확인해 주세요."
    answer_html = _md_to_html(answer_main)

    # ---- 디버그 로그 ----
    if getattr(settings, "DEBUG", False):
        try:
            log.info(
                "PDF analyze: staff=%s files_in=%d files_used=%d mode=%s qlen=%d text_len=%d errors=%d limits=(files=%d,sizeMB=%d,totalChars=%d)",
                "Y" if is_staff else "N",
                len(files),
                len(extracted_blocks),
                mode,
                len(question),
                len(combined_text),
                len(file_errors),
                max_files,
                max_size_mb,
                max_total_chars,
            )
        except Exception:
            pass

    return JsonResponse(
        {
            "ok": True,
            "answer_text": answer_main,
            "answer_html": answer_html,
            "mode": mode,
            "files_used": [fn for fn, _ in extracted_blocks],
            "file_errors": file_errors,
            "is_staff": is_staff,
        }
    )
