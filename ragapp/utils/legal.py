# ragapp/utils/legal.py
from __future__ import annotations

import os
import time
from functools import lru_cache
from typing import Optional, Tuple, Any, Dict

from django.apps import apps
from django.core.exceptions import FieldError
from django.db import models

# 모델 임포트 (없어도 동작하게 가드)
try:
    from ragapp.models import LegalConfig, sanitize_legal_html  # type: ignore
except Exception:  # pragma: no cover
    LegalConfig = None  # type: ignore

    def sanitize_legal_html(html: str) -> str:  # type: ignore
        return html


# ─────────────────────────────────────────────
# DB table 존재 확인 (앱 준비 전 DB 접근 방지)
# - TOPLEVEL에 connection을 import하지 않음 (스캐너 회피)
# ─────────────────────────────────────────────
@lru_cache(maxsize=128)
def _has_table_cached(table_name: str) -> bool:
    from django.db import connection

    try:
        vendor = getattr(connection, "vendor", "")
        with connection.cursor() as c:
            if vendor == "sqlite":
                c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=%s",
                    [table_name],
                )
                return c.fetchone() is not None

            # 다른 DB(POSTGRES/MYSQL 등)도 최대한 안전하게
            try:
                names = connection.introspection.table_names(cursor=c)
                return table_name in set(names or [])
            except Exception:
                return False
    except Exception:
        return False


def _has_table(name: str) -> bool:
    # ✅ 앱 초기화 중(특히 system check/ready/import)에는 DB 접근 금지
    if not apps.ready:
        return False
    return bool(_has_table_cached(name))


def _truthy(v: Any) -> bool:
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ("on", "1", "true", "yes", "y")


# ─────────────────────────────────────────────
# (구버전 호환) enabled 별칭 제공용 Manager
# - 필요할 때만 적용(런타임), import 시 DB 접근 없음
# ─────────────────────────────────────────────
class _LegalConfigManager(models.Manager):
    """
    기존 코드에서 filter(enabled=True)를 쓰던 흔적이 있으면,
    enabled = consent_gate_enabled 별칭을 annotate로 제공해 호환.
    """
    def get_queryset(self):
        qs = super().get_queryset()
        try:
            return qs.annotate(enabled=models.F("consent_gate_enabled"))
        except Exception:
            return qs


def _patch_manager_if_possible() -> None:
    """
    런타임에서 기본 매니저를 _LegalConfigManager로 교체.
    - 앱 준비 전/테이블 없으면 아무 것도 안 함
    - 실패해도 치명적이지 않게 무시
    """
    if LegalConfig is None:
        return
    if not _has_table("ragapp_legalconfig"):
        return
    try:
        LegalConfig.add_to_class("objects", _LegalConfigManager())
        LegalConfig._default_manager = LegalConfig.objects  # type: ignore[attr-defined]
    except Exception:
        pass


def _active_legal_qs():
    """
    LegalConfig를 안전하게 조회하는 헬퍼.
    - 새로운 스키마: consent_gate_enabled=True 우선
    - 구 스키마: enabled=True(별칭)도 허용
    - 다 아니면 최신(updated_at DESC) 1개로 폴백
    """
    if LegalConfig is None or not _has_table("ragapp_legalconfig"):
        return None

    _patch_manager_if_possible()

    qs = LegalConfig.objects.order_by("-updated_at")

    # 신필드 우선
    try:
        q = qs.filter(consent_gate_enabled=True)
        if q.exists():
            return q
    except FieldError:
        pass

    # 구필드(호환)
    try:
        q = qs.filter(enabled=True)  # type: ignore[attr-defined]
        if q.exists():
            return q
    except FieldError:
        pass

    return qs


def get_active_legal_config() -> Optional[Any]:
    """
    활성화된 LegalConfig 1개 반환(없으면 최신 1개, 없으면 None)
    """
    qs = _active_legal_qs()
    if qs is None:
        return None
    return qs.first()


# ─────────────────────────────────────────────
# TTL 캐시: 요청마다 DB 치지 않게
# ─────────────────────────────────────────────
_LEGALCFG_CACHE_TTL = int(os.environ.get("LEGALCFG_CACHE_TTL", "30"))  # seconds
_legalcfg_cache: Dict[str, Any] = {"ts": 0.0, "val": None}


def get_active_legal_config_cached() -> Optional[Any]:
    """
    - apps.ready 전이면 None
    - TTL 동안은 캐시 반환
    """
    if not apps.ready:
        return None

    now = time.time()
    if (now - float(_legalcfg_cache["ts"])) < _LEGALCFG_CACHE_TTL:
        return _legalcfg_cache["val"]

    val = get_active_legal_config()
    _legalcfg_cache["ts"] = now
    _legalcfg_cache["val"] = val
    return val


def build_legal_context() -> dict:
    """
    템플릿에 넣을 법무/서비스 컨텍스트 공통 생성기.
    - 소문자 키: 기본
    - 대문자 키: 구 템플릿 호환(SERVICE_NAME 등)
    """
    try:
        cfg = get_active_legal_config_cached()
    except Exception:
        cfg = None

    service_name = getattr(cfg, "service_name", None) or "AI 뉴스 분석 콘솔"
    effective_date_obj = getattr(cfg, "effective_date", None)
    effective_date = (
        effective_date_obj.isoformat()
        if getattr(effective_date_obj, "isoformat", None)
        else "2025-11-02"
    )

    operator_name = getattr(cfg, "operator_name", None) or "김동건"
    contact_email = getattr(cfg, "contact_email", None) or "privacy@example.com"
    contact_phone = getattr(cfg, "contact_phone", None) or ""

    privacy_html = sanitize_legal_html(getattr(cfg, "privacy_html", "") if cfg else "")
    cross_border_html = sanitize_legal_html(getattr(cfg, "cross_border_html", "") if cfg else "")
    tester_html = sanitize_legal_html(getattr(cfg, "tester_html", "") if cfg else "")

    return {
        "legal_config": cfg,
        # 소문자(기본)
        "service_name": service_name,
        "effective_date": effective_date,
        "operator_name": operator_name,
        "contact_email": contact_email,
        "contact_phone": contact_phone,
        "privacy_html": privacy_html,
        "cross_border_html": cross_border_html,
        "tester_html": tester_html,
        # 대문자(구 템플릿 호환)
        "SERVICE_NAME": service_name,
        "EFFECTIVE_DATE": effective_date,
        "OPERATOR_NAME": operator_name,
        "CONTACT_EMAIL": contact_email,
        "CONTACT_PHONE": contact_phone,
        "PRIVACY_HTML": privacy_html,
        "CROSS_BORDER_HTML": cross_border_html,
        "TESTER_HTML": tester_html,
    }


# ─────────────────────────────────────────────
# 서버 측 동의 체크
# ─────────────────────────────────────────────
def validate_required_consents(request) -> Tuple[bool, Optional[str]]:
    """
    서버 측 동의 게이트 체크.
    - consent_gate_enabled(또는 구호환 enabled)가 True면 동의 필요
    - 세션/쿠키에 동의 흔적 있으면 통과
    """
    # 세션/쿠키 동의 흔적 있으면 OK
    try:
        if request.session.get("consent_ok") in (True, "1", "on"):
            return True, None
        for k in ("consent_ok", "consent_required", "agree_privacy"):
            if _truthy(request.COOKIES.get(k)):
                return True, None
    except Exception:
        pass

    cfg = get_active_legal_config_cached()
    if cfg is None:
        return True, None

    # 게이트 on인지 확인(필드 존재 유무 안전)
    gate_on = False
    if hasattr(cfg, "consent_gate_enabled"):
        gate_on = bool(getattr(cfg, "consent_gate_enabled"))
    elif hasattr(cfg, "enabled"):
        gate_on = bool(getattr(cfg, "enabled"))

    if not gate_on:
        return True, None

    return False, "❌ 개인정보 수집·이용(필수)에 동의해 주세요."
