# ragapp/news_views/context_processors.py
from __future__ import annotations

from typing import Dict, Any, Optional

from django.conf import settings
from django.utils import timezone

# (있으면) 오늘 사용량을 DB에서 읽어서 "남은"까지 계산
try:
    from ragapp.models import DailyUsage  # type: ignore
except Exception:  # pragma: no cover
    DailyUsage = None  # type: ignore

try:
    from ragapp.services.usage_limiter import build_client_key  # type: ignore
except Exception:  # pragma: no cover
    build_client_key = None  # type: ignore


def _get_int_setting(name: str, default: int) -> int:
    try:
        v = getattr(settings, name, default)
        return int(v)
    except Exception:
        return default


def _is_admin_unlimited(request) -> bool:
    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        return False
    return bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))


def _get_today_usage_row(request) -> Optional[Any]:
    """
    오늘자 DailyUsage row (없으면 None)
    """
    if DailyUsage is None or not callable(build_client_key):
        return None
    try:
        today = timezone.localdate()
        key = build_client_key(request)
        return (
            DailyUsage.objects.filter(date=today, client_key=key)
            .only("web_count", "rag_count", "pdf_count", "image_count", "table_count")
            .first()
        )
    except Exception:
        return None


def _calc_remaining(limit: int, used: int, unlimited: bool) -> tuple[Optional[int], str]:
    if unlimited or limit <= 0:
        return None, "∞"
    rem = max(limit - used, 0)
    return rem, str(rem)


def usage_limits(request) -> Dict[str, Any]:
    """
    오늘 사용량 위젯에서 쓸 값:
      - 제한값(limit)
      - 사용량(used)  (DailyUsage가 있으면)
      - 남은횟수(remaining) (가능하면)

    템플릿 호환:
      - USAGE_LIMIT_WEB/RAG/PDF (+ IMAGE/TABLE 추가)
      - 남은횟수는 USAGE_REMAIN_* / USAGE_REMAIN_*_TXT 로 제공
    """
    # ✅ settings.py에서 매핑해둔 값을 읽는 방식 유지
    web_limit = _get_int_setting("USAGE_LIMIT_WEB_DAILY", 0)
    rag_limit = _get_int_setting("USAGE_LIMIT_RAG_DAILY", 0)
    pdf_limit = _get_int_setting("USAGE_LIMIT_PDF_DAILY", 0)
    image_limit = _get_int_setting("USAGE_LIMIT_IMAGE_DAILY", 0)
    table_limit = _get_int_setting("USAGE_LIMIT_TABLE_DAILY", 0)

    admin_unlimited = _is_admin_unlimited(request)

    # 기본 used=0 (DB 못 읽으면 0으로)
    used_web = used_rag = used_pdf = used_image = used_table = 0

    if not admin_unlimited:
        row = _get_today_usage_row(request)
        if row is not None:
            used_web = int(getattr(row, "web_count", 0) or 0)
            used_rag = int(getattr(row, "rag_count", 0) or 0)
            used_pdf = int(getattr(row, "pdf_count", 0) or 0)
            used_image = int(getattr(row, "image_count", 0) or 0)
            used_table = int(getattr(row, "table_count", 0) or 0)

    # unlimited 판단: (관리자) or (limit<=0)
    un_web = bool(admin_unlimited or web_limit <= 0)
    un_rag = bool(admin_unlimited or rag_limit <= 0)
    un_pdf = bool(admin_unlimited or pdf_limit <= 0)
    un_image = bool(admin_unlimited or image_limit <= 0)
    un_table = bool(admin_unlimited or table_limit <= 0)

    rem_web, rem_web_txt = _calc_remaining(web_limit, used_web, un_web)
    rem_rag, rem_rag_txt = _calc_remaining(rag_limit, used_rag, un_rag)
    rem_pdf, rem_pdf_txt = _calc_remaining(pdf_limit, used_pdf, un_pdf)
    rem_image, rem_image_txt = _calc_remaining(image_limit, used_image, un_image)
    rem_table, rem_table_txt = _calc_remaining(table_limit, used_table, un_table)

    return {
        # 기존 limit 키
        "USAGE_LIMIT_WEB": web_limit,
        "USAGE_LIMIT_RAG": rag_limit,
        "USAGE_LIMIT_PDF": pdf_limit,
        # ✅ 추가
        "USAGE_LIMIT_IMAGE": image_limit,
        "USAGE_LIMIT_TABLE": table_limit,

        # used (템플릿에서 “사용/남음” 같이 보여줄 때 씀)
        "USAGE_USED_WEB": used_web,
        "USAGE_USED_RAG": used_rag,
        "USAGE_USED_PDF": used_pdf,
        "USAGE_USED_IMAGE": used_image,
        "USAGE_USED_TABLE": used_table,

        # remaining (정수 or None)
        "USAGE_REMAIN_WEB": rem_web,
        "USAGE_REMAIN_RAG": rem_rag,
        "USAGE_REMAIN_PDF": rem_pdf,
        "USAGE_REMAIN_IMAGE": rem_image,
        "USAGE_REMAIN_TABLE": rem_table,

        # remaining 표시용(무제한이면 ∞)
        "USAGE_REMAIN_WEB_TXT": rem_web_txt,
        "USAGE_REMAIN_RAG_TXT": rem_rag_txt,
        "USAGE_REMAIN_PDF_TXT": rem_pdf_txt,
        "USAGE_REMAIN_IMAGE_TXT": rem_image_txt,
        "USAGE_REMAIN_TABLE_TXT": rem_table_txt,

        # unlimited flags
        "USAGE_UNLIMITED_WEB": un_web,
        "USAGE_UNLIMITED_RAG": un_rag,
        "USAGE_UNLIMITED_PDF": un_pdf,
        "USAGE_UNLIMITED_IMAGE": un_image,
        "USAGE_UNLIMITED_TABLE": un_table,

        "USAGE_WIDGET_ENABLED": True,
    }
