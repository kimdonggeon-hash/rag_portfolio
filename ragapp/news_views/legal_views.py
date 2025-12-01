# ragapp/news_views/legal_views.py
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string, select_template
from django.utils import timezone

# LegalConfig가 없어도 안전 동작
try:
    from ragapp.models import LegalConfig  # type: ignore
except Exception:  # pragma: no cover
    LegalConfig = None  # type: ignore


# ------------------------------
# Helpers
# ------------------------------
def _bool_env(name: str, default: bool) -> bool:
    raw = str(getattr(settings, name, os.environ.get(name, str(default)))).strip().lower()
    return raw not in ("0", "false", "no", "", "none")

def _int_env(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, os.environ.get(name, default)))
    except Exception:
        return default

def _get_active_cfg_obj() -> Tuple[Optional[Any], Optional[Any]]:
    """
    Returns: (LegalConfig instance or None, last_updated or None)
    - is_active / active / enabled 컬럼이 있으면 True 우선
    """
    if not LegalConfig:
        return None, None
    try:
        qs = LegalConfig.objects.all()
        for flag in ("is_active", "active", "enabled"):
            if hasattr(LegalConfig, flag):
                try:
                    qs = qs.filter(**{flag: True}) or qs
                except Exception:
                    pass
                break
        inst = qs.order_by("-id").first()
        last_updated = getattr(inst, "updated_at", None) if inst else None
        return inst, last_updated
    except Exception:
        return None, None

def _get_contact_email() -> str:
    inst, _ = _get_active_cfg_obj()
    # 필드 여러 이름 대응
    for name in ("contact_email", "email", "support_email", "admin_email"):
        if inst and hasattr(inst, name):
            val = getattr(inst, name)
            if val:
                return str(val)
    return os.environ.get("CONTACT_EMAIL") or getattr(settings, "CONTACT_EMAIL", "") or ""

def _build_cfg_dict() -> Tuple[Dict[str, Any], Any]:
    """
    privacy.html이 필요로 하는 풍부한 컨텍스트(dict)를 구성.
    - LegalConfig 값을 우선 사용하고 없으면 settings/env 폴백
    - 문의 링크(contact_link)는 URL > mailto > '#'
    Returns: (cfg dict, last_updated)
    """
    inst, last_updated = _get_active_cfg_obj()

    cfg: Dict[str, Any] = {}

    # 1) LegalConfig 우선
    if inst:
        for k in (
            "service_name",
            "operator_name",
            "contact_email",
            "summary_only",
            "store_fulltext",
            "robots_on",
            "safe_mode_enabled",
            "log_ip_hashed",
            "retention_days",
            "model_name",
            "contact_url",
        ):
            if hasattr(inst, k):
                cfg[k] = getattr(inst, k)

    # 2) settings / env 폴백
    cfg.setdefault("service_name", getattr(settings, "SERVICE_NAME", os.environ.get("SERVICE_NAME", "RAG 통합 검색 콘솔")))
    cfg.setdefault("operator_name", getattr(settings, "OPERATOR_NAME", os.environ.get("OPERATOR_NAME", "운영자")))
    cfg.setdefault("contact_email", _get_contact_email())

    cfg.setdefault("summary_only", _bool_env("SUMMARY_ONLY", False))
    cfg.setdefault("store_fulltext", _bool_env("STORE_FULLTEXT", True))
    cfg.setdefault("robots_on", _bool_env("ROBOTS_ON", True))
    cfg.setdefault("safe_mode_enabled", _bool_env("SAFE_MODE_ENABLED", True))
    cfg.setdefault("log_ip_hashed", _bool_env("LOG_IP_HASHED", True))
    cfg.setdefault("retention_days", _int_env("RETENTION_DAYS", 90))
    cfg.setdefault("model_name", getattr(settings, "GEN_MODEL_NAME", os.environ.get("GEN_MODEL_NAME", "gemini-2.5-flash")))

    # 3) 문의 링크(우선순위: 설정 URL > mailto > '#')
    contact_url = cfg.get("contact_url") or getattr(settings, "LEGAL_CONTACT_URL", os.environ.get("LEGAL_CONTACT_URL"))
    if not contact_url:
        email = cfg.get("contact_email") or ""
        contact_url = f"mailto:{email}" if email else "#"
    cfg["contact_link"] = contact_url

    return cfg, (last_updated or timezone.now())


def _render_slot_page(
    request: HttpRequest,
    template_candidates: list[str],
    slot_key_in_db: str,
    slot_key_in_settings: str,
    extra_ctx: Optional[Dict[str, Any]] = None,
) -> HttpResponse:
    """
    - 템플릿 파일이 있으면 파일로 렌더링
    - 템플릿이 없어도 동작해야 하는 경우를 위해 raw HTML 슬롯도 컨텍스트에 포함
      (templates에서 {{ raw_html|safe }} 형태로 사용할 수 있음)
    """
    inst, _ = _get_active_cfg_obj()
    raw_html = ""
    if inst and hasattr(inst, slot_key_in_db):
        raw_html = getattr(inst, slot_key_in_db) or ""
    if not raw_html:
        raw_html = getattr(settings, slot_key_in_settings, os.environ.get(slot_key_in_settings, ""))

    base_cfg, last_updated = _build_cfg_dict()
    ctx = {
        "cfg": base_cfg,
        "last_updated": last_updated,
        "raw_html": raw_html,  # 템플릿이 직접 쓰고 싶을 때
        # 하위호환: 기존 키 유지
        slot_key_in_db: raw_html,
        slot_key_in_settings: raw_html,
    }
    if extra_ctx:
        ctx.update(extra_ctx)

    tpl = select_template(template_candidates)
    return HttpResponse(tpl.render(ctx, request))


# ------------------------------
# Pages
# ------------------------------
def legal_privacy(request: HttpRequest) -> HttpResponse:
    """
    개인정보 처리 안내
    - 개선된 privacy.html이 있으면 그걸 사용
    - 없더라도 기존 슬롯 기반 템플릿과 호환되도록 컨텍스트 제공
    """
    return _render_slot_page(
        request,
        template_candidates=[
            "legal/privacy.html",
            "ragapp/legal/privacy.html",
            "legal/privacy_policy.html",
        ],
        slot_key_in_db="privacy_html",
        slot_key_in_settings="LEGAL_PRIVACY_HTML",
    )


def legal_tos(request: HttpRequest) -> HttpResponse:
    return _render_slot_page(
        request,
        template_candidates=[
            "legal/tos.html",
            "ragapp/legal/tos.html",
        ],
        slot_key_in_db="tos_html",
        slot_key_in_settings="LEGAL_TOS_HTML",
    )


def legal_overseas(request: HttpRequest) -> HttpResponse:
    return _render_slot_page(
        request,
        template_candidates=[
            "legal/overseas.html",
            "ragapp/legal/overseas.html",
        ],
        slot_key_in_db="overseas_html",
        slot_key_in_settings="LEGAL_OVERSEAS_HTML",
    )


def legal_tester(request: HttpRequest) -> HttpResponse:
    return _render_slot_page(
        request,
        template_candidates=[
            "legal/tester.html",
            "ragapp/legal/tester.html",
        ],
        slot_key_in_db="tester_html",
        slot_key_in_settings="LEGAL_TESTER_HTML",
    )


def legal_guide(request: HttpRequest) -> HttpResponse:
    return _render_slot_page(
        request,
        template_candidates=[
            "legal/guide.html",
            "ragapp/legal/guide.html",
        ],
        slot_key_in_db="guide_html",
        slot_key_in_settings="LEGAL_GUIDE_HTML",
    )


# ------------------------------
# JSON bundle (모달/오버레이 하이드레이션용)
# ------------------------------
def legal_bundle(request: HttpRequest) -> JsonResponse:
    """
    news.html 등에서 모달 하이드레이션 용도로 사용.
    - 템플릿이 있으면 렌더링 결과를, 없으면 슬롯 raw_html을 반환
    """
    base_cfg, last_updated = _build_cfg_dict()

    def _safe_html(tpl_name: str, db_key: str, settings_key: str) -> str:
        # 1) 템플릿이 있으면 렌더 반환
        try:
            tpl = select_template([tpl_name, f"ragapp/{tpl_name}"])
            return tpl.render({"cfg": base_cfg, "last_updated": last_updated})
        except Exception:
            pass
        # 2) 슬롯 기반 raw html 반환
        inst, _ = _get_active_cfg_obj()
        if inst and hasattr(inst, db_key):
            try:
                html = getattr(inst, db_key) or ""
                if html:
                    return html
            except Exception:
                pass
        return getattr(settings, settings_key, os.environ.get(settings_key, "")) or ""

    return JsonResponse(
        {
            "privacy_html": _safe_html("legal/privacy.html", "privacy_html", "LEGAL_PRIVACY_HTML"),
            "tos_html": _safe_html("legal/tos.html", "tos_html", "LEGAL_TOS_HTML"),
            "overseas_html": _safe_html("legal/overseas.html", "overseas_html", "LEGAL_OVERSEAS_HTML"),
            "tester_html": _safe_html("legal/tester.html", "tester_html", "LEGAL_TESTER_HTML"),
            "guide_html": _safe_html("legal/guide.html", "guide_html", "LEGAL_GUIDE_HTML"),
        }
    )


__all__ = [
    "legal_privacy",
    "legal_tos",
    "legal_overseas",
    "legal_tester",
    "legal_guide",
    "legal_bundle",
]
