# ragapp/templatetags/legal_extras.py
from __future__ import annotations

from django import template
from django.utils.safestring import mark_safe

# LegalConfig / sanitize_legal_html 유무와 상관없이 안전하게 동작
try:
    from ragapp.models import LegalConfig, sanitize_legal_html  # type: ignore
except Exception:  # pragma: no cover
    LegalConfig = None  # type: ignore

    def sanitize_legal_html(html: str) -> str:  # type: ignore
        return html

register = template.Library()


def _get_cfg():
    """
    LegalConfig 안전 조회:
    - get_solo() 있으면 사용
    - 없으면 updated_at DESC, id DESC 로 1건
    - 없거나 실패하면 None
    """
    try:
        if not LegalConfig:
            return None
        if hasattr(LegalConfig, "get_solo"):
            return LegalConfig.get_solo()  # type: ignore[attr-defined]
        return LegalConfig.objects.order_by("-updated_at", "-id").first()
    except Exception:
        return None


@register.simple_tag
def legal_title(slug: str | None = None) -> str:  # slug는 호환용(무시)
    cfg = _get_cfg()
    return getattr(cfg, "service_name", "서비스 안내")


@register.simple_tag
def legal_operator() -> str:
    cfg = _get_cfg()
    return getattr(cfg, "operator_name", "")


@register.simple_tag
def legal_contact_email() -> str:
    cfg = _get_cfg()
    return getattr(cfg, "contact_email", "")


@register.simple_tag
def legal_contact_phone() -> str:
    cfg = _get_cfg()
    return getattr(cfg, "contact_phone", "")


@register.simple_tag
def legal_effective_date(fmt: str = "%Y-%m-%d") -> str:
    cfg = _get_cfg()
    dt = getattr(cfg, "effective_date", None)
    if not dt:
        return ""
    try:
        return dt.strftime(fmt)
    except Exception:
        return str(dt)


# 개별 HTML 바로 꺼내는 태그들(선택적으로 사용)
@register.simple_tag
def legal_privacy_html():
    cfg = _get_cfg()
    html = getattr(cfg, "privacy_html", "") or ""
    return mark_safe(sanitize_legal_html(html))


@register.simple_tag
def legal_cross_border_html():
    cfg = _get_cfg()
    html = getattr(cfg, "cross_border_html", "") or ""
    return mark_safe(sanitize_legal_html(html))


@register.simple_tag
def legal_tester_html():
    cfg = _get_cfg()
    html = getattr(cfg, "tester_html", "") or ""
    return mark_safe(sanitize_legal_html(html))


@register.simple_tag
def legal_guide_html():
    cfg = _get_cfg()
    html = getattr(cfg, "guide_html", "") or ""
    return mark_safe(sanitize_legal_html(html))


@register.simple_tag
def legal_html(kind: str) -> str:
    """
    템플릿에서 하나로 쓰기 쉽게 만든 통합 태그.
    사용 예:
      {% legal_html 'privacy' %}
      {% legal_html 'cross' %}
      {% legal_html 'tester' %}
      {% legal_html 'guide' %}
    """
    cfg = _get_cfg()
    field_map = {
        "privacy": "privacy_html",
        "cross": "cross_border_html",
        "cross_border": "cross_border_html",
        "tester": "tester_html",
        "guide": "guide_html",
    }
    field = field_map.get((kind or "").strip().lower())
    if not field:
        return ""
    html = getattr(cfg, field, "") if cfg else ""
    return mark_safe(sanitize_legal_html(html or ""))


@register.simple_tag
def legal_gate_enabled() -> bool:
    """
    동의 게이트 스위치 반환.
    - 새 스키마: consent_gate_enabled
    - 구 스키마: enabled (있다면)
    """
    cfg = _get_cfg()
    if not cfg:
        return False
    if hasattr(cfg, "consent_gate_enabled"):
        return bool(getattr(cfg, "consent_gate_enabled"))
    if hasattr(cfg, "enabled"):  # 구버전 호환
        return bool(getattr(cfg, "enabled"))
    return False
