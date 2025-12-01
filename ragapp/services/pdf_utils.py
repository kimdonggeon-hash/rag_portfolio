# ragapp/services/pdf_utils.py
from __future__ import annotations
import io
import tempfile
from typing import Optional, List

# 우선순위 1: pypdf
try:
    from pypdf import PdfReader as _PdfReader  # type: ignore
except Exception:  # 우선순위 2: PyPDF2
    try:
        from PyPDF2 import PdfReader as _PdfReader  # type: ignore
    except Exception:
        _PdfReader = None  # type: ignore

# 우선순위 3: pdfminer.six (없으면 None)
try:
    from pdfminer.high_level import extract_text as _pdfminer_extract_text  # type: ignore
except Exception:
    _pdfminer_extract_text = None  # type: ignore


def _extract_with_pypdf(data: bytes, max_pages: Optional[int] = None) -> str:
    if _PdfReader is None:
        return ""
    reader = _PdfReader(io.BytesIO(data))
    parts: List[str] = []
    for i, page in enumerate(getattr(reader, "pages", [])):
        if max_pages is not None and i >= max_pages:
            break
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        if t:
            parts.append(t)
    return "\n".join(parts).strip()


def _extract_with_pdfminer(data: bytes) -> str:
    if _pdfminer_extract_text is None:
        return ""
    # pdfminer는 파일 경로 입력이 편해서 임시 파일로 우회
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(data)
        tmp.flush()
        try:
            return (_pdfminer_extract_text(tmp.name) or "").strip()
        except Exception:
            return ""


def extract_text_from_pdf_bytes(data: bytes, max_pages: Optional[int] = None) -> str:
    """
    PDF 바이트에서 텍스트를 추출.
    1) pypdf/PyPDF2 → 2) pdfminer.six 순으로 시도, 모두 실패 시 빈 문자열.
    """
    text = _extract_with_pypdf(data, max_pages=max_pages)
    if text:
        return text
    text = _extract_with_pdfminer(data)
    return text or ""


# (옵션) 경로 버전이 필요하면 이것도 사용 가능
def extract_text_from_pdf(path: str, max_pages: Optional[int] = None) -> str:
    with open(path, "rb") as f:
        return extract_text_from_pdf_bytes(f.read(), max_pages=max_pages)
