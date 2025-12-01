# ragapp/context_processors.py
from __future__ import annotations
from datetime import date
from typing import Any, Dict
import os
from pathlib import Path
from django.conf import settings
from django.utils import timezone

try:
    from .models import LegalConfig
except Exception:
    LegalConfig = None  # 마이그레이션 전 안전 폴백


def vectordb_paths(_request):
    return {
        "VECTOR_DB_PATH": getattr(
            settings,
            "VECTOR_DB_PATH",
            getattr(settings, "CHROMA_DB_DIR", ""),
        ),
        "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),
        "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
    }


def app_constants(_request):
    return {
        "VECTOR_DB_PATH": os.environ.get("VECTOR_DB_PATH")
        or str(Path(getattr(settings, "BASE_DIR", ".")) / "vector_store.sqlite3"),
        # (호환용 – 템플릿에 남아있을 수 있는 키)
        "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),
        "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
    }


def static_version(_request) -> Dict[str, Any]:
    """
    정적 파일 캐시 버전용 컨텍스트.
    - 우선순위:
      1) settings.STATIC_VERSION
      2) 환경변수 STATIC_VERSION
      3) 오늘 날짜(YYYYMMDD) 또는 'dev'
    """
    raw = getattr(settings, "STATIC_VERSION", None) or os.environ.get("STATIC_VERSION")
    if isinstance(raw, str) and raw.strip():
        ver = raw.strip()
    else:
        try:
            ver = timezone.now().strftime("%Y%m%d")
        except Exception:
            ver = "dev"

    return {"STATIC_VERSION": ver}


def _get_privacy_url() -> str:
    return getattr(settings, "PRIVACY_PAGE_URL", "/privacy")


def legal_context(request) -> Dict[str, Any]:
    """
    법적/동의 관련 전역 컨텍스트.
    - 새 모델(LegalConfig)에 맞춰 안전하게 기본값 제공
    - 예전 템플릿/JS가 기대하던 키(require_checkbox 등)도 폴백으로 채워줌
    """
    cfg = None
    if LegalConfig is not None:
        try:
            # 단일 레코드 사용 권장: 없으면 자동 생성
            cfg = LegalConfig.get_solo()
        except Exception:
            cfg = None

    # 안전한 기본값
    service_name = (getattr(cfg, "service_name", None) or "AI 뉴스 분석 콘솔")
    effective_date = getattr(cfg, "effective_date", None) or date.today()
    operator_name = (getattr(cfg, "operator_name", None) or "운영자")
    contact_email = (getattr(cfg, "contact_email", None) or "privacy@example.com")
    contact_phone = getattr(cfg, "contact_phone", "") or ""

    consent_gate_enabled = bool(getattr(cfg, "consent_gate_enabled", True))

    # HTML 원문(있으면 sanitize된 프로퍼티 우선)
    privacy_html = (
        getattr(cfg, "sanitized_privacy_html", "")
        or getattr(cfg, "privacy_html", "")
        or ""
    )
    cross_border_html = (
        getattr(cfg, "sanitized_cross_border_html", "")
        or getattr(cfg, "cross_border_html", "")
        or ""
    )
    tester_html = (
        getattr(cfg, "sanitized_tester_html", "")
        or getattr(cfg, "tester_html", "")
        or ""
    )

    # ✅ 구버전 호환 키 (예전 코드에서 참조해도 터지지 않도록)
    require_checkbox = True  # 동의 체크박스 사용
    require_modal = False  # 전체 화면 강제 모달 여부 (사용 안 하면 False)
    privacy_page_url = _get_privacy_url()

    # 전역 평면 키(템플릿에서 바로 씀)
    flat: Dict[str, Any] = {
        "service_name": service_name,
        "effective_date": effective_date,
        "operator_name": operator_name,
        "contact_email": contact_email,
        "contact_phone": contact_phone,
        "consent_gate_enabled": consent_gate_enabled,
        "PRIVACY_PAGE_URL": privacy_page_url,
        # 선택: 페이지에서 직접 렌더링할 때 사용 가능
        "privacy_html": privacy_html,
        "cross_border_html": cross_border_html,
        "tester_html": tester_html,
        # 구버전 호환
        "require_checkbox": require_checkbox,
        "require_modal": require_modal,
    }

    # JS에서 window.LEGAL 같은 객체로 쓰고 싶을 때를 위해 중첩도 함께 제공
    flat["LEGAL"] = {
        "service_name": service_name,
        "effective_date": effective_date.isoformat()
        if hasattr(effective_date, "isoformat")
        else str(effective_date),
        "operator_name": operator_name,
        "contact_email": contact_email,
        "contact_phone": contact_phone,
        "consent_gate_enabled": consent_gate_enabled,
        "privacy_page_url": privacy_page_url,
        "require_checkbox": require_checkbox,
        "require_modal": require_modal,
    }

    return flat
