# ragapp/news_views/legal_views.py
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any, Dict, Optional, Tuple, List

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.template import TemplateDoesNotExist
from django.template.loader import select_template
from django.utils import timezone

from ragapp.services import legal_tables

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
    return raw not in ("0", "false", "no", "", "none", "null", "off")


def _int_env(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, os.environ.get(name, default)))
    except Exception:
        return default


def _to_datestr(v: Any) -> str:
    """
    템플릿에서 그대로 출력해도 안전한 YYYY-MM-DD 문자열로 통일.
    (date filter를 쓰지 않아도 됨)
    """
    if v in (None, ""):
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, datetime):
        return timezone.localtime(v).date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    try:
        return str(v)
    except Exception:
        return ""


def _get_active_cfg_obj() -> Tuple[Optional[Any], Optional[Any]]:
    """
    Returns: (LegalConfig instance or None, last_updated or None)
    - is_active / active / enabled 컬럼이 있으면 True 우선
    """
    if not LegalConfig:
        return None, None

    try:
        qs = LegalConfig.objects.all()

        # ✅ QuerySet "or" 제거 (truthy 평가 이슈 방지)
        for flag in ("is_active", "active", "enabled"):
            if hasattr(LegalConfig, flag):
                try:
                    qs = qs.filter(**{flag: True})
                except Exception:
                    pass
                break

        # 최신 우선 정렬
        if hasattr(LegalConfig, "updated_at"):
            qs = qs.order_by("-updated_at", "-id")
        else:
            qs = qs.order_by("-id")

        inst = qs.first()
        last_updated = getattr(inst, "updated_at", None) if inst else None
        return inst, last_updated
    except Exception:
        return None, None


def _get_contact_email() -> str:
    inst, _ = _get_active_cfg_obj()
    for name in ("contact_email", "email", "support_email", "admin_email"):
        if inst and hasattr(inst, name):
            val = getattr(inst, name, None)
            if val:
                return str(val)
    return os.environ.get("CONTACT_EMAIL") or getattr(settings, "CONTACT_EMAIL", "") or ""


def _build_cfg_dict() -> Tuple[Dict[str, Any], Any]:
    """
    legal 템플릿들이 필요로 하는 풍부한 컨텍스트(dict)를 구성.
    - LegalConfig 값을 우선 사용하고 없으면 settings/env 폴백
    Returns: (cfg dict, last_updated)
    """
    inst, last_updated = _get_active_cfg_obj()
    cfg: Dict[str, Any] = {}

    # 1) LegalConfig 우선 (있는 필드만 안전하게)
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
            # ✅ ToS/시행일(네이밍 여러 버전 대응)
            "tos_version",
            "tos_effective_date",
            "terms_version",
            "terms_effective_date",
            "effective_date",
            # ✅ Tester(테스터 동의서)
            "tester_version",
            "tester_effective_date",
            # ✅ 원칙 문구(네이밍 여러 버전 대응)
            "policy_minimization",
            "policy_processing_location",
            "policy_change_note",
        ):
            if hasattr(inst, k):
                cfg[k] = getattr(inst, k)

    # 2) settings / env 폴백 (기본 식별자)  ✅ 빈 문자열이면 덮어쓰기
    if not str(cfg.get("service_name") or "").strip():
        cfg["service_name"] = getattr(
            settings, "SERVICE_NAME", os.environ.get("SERVICE_NAME", "RAG 통합 검색 콘솔")
        )
    if not str(cfg.get("operator_name") or "").strip():
        cfg["operator_name"] = getattr(
            settings, "OPERATOR_NAME", os.environ.get("OPERATOR_NAME", "운영자")
        )
    if not str(cfg.get("contact_email") or "").strip():
        cfg["contact_email"] = _get_contact_email()

    # bool 필드: None이면만 폴백(빈 문자열/0 들어오면 _bool_env가 판단하도록)
    if cfg.get("summary_only") is None:
        cfg["summary_only"] = _bool_env("SUMMARY_ONLY", False)
    if cfg.get("store_fulltext") is None:
        cfg["store_fulltext"] = _bool_env("STORE_FULLTEXT", True)
    if cfg.get("robots_on") is None:
        cfg["robots_on"] = _bool_env("ROBOTS_ON", True)
    if cfg.get("safe_mode_enabled") is None:
        cfg["safe_mode_enabled"] = _bool_env("SAFE_MODE_ENABLED", True)
    if cfg.get("log_ip_hashed") is None:
        cfg["log_ip_hashed"] = _bool_env("LOG_IP_HASHED", True)

    # retention_days: 0/빈값이면 의미 없으니 기본값으로 보정
    try:
        rd_raw = cfg.get("retention_days")
        rd = int(rd_raw) if rd_raw not in (None, "", 0, "0") else _int_env("RETENTION_DAYS", 90)
    except Exception:
        rd = _int_env("RETENTION_DAYS", 90)
    cfg["retention_days"] = rd if rd > 0 else 90

    if not str(cfg.get("model_name") or "").strip():
        cfg["model_name"] = getattr(
            settings, "GEN_MODEL_NAME", os.environ.get("GEN_MODEL_NAME", "gemini-3.5-flash")
        )

    # 2-1) 자주 쓰는 내부 링크 기본값(템플릿 안정화)
    if not str(cfg.get("tos_url") or "").strip():
        cfg["tos_url"] = "/legal/tos/"
    if not str(cfg.get("privacy_url") or "").strip():
        cfg["privacy_url"] = "/legal/privacy/"
    if not str(cfg.get("overseas_url") or "").strip():
        cfg["overseas_url"] = "/legal/overseas/"
    if not str(cfg.get("tester_url") or "").strip():
        cfg["tester_url"] = "/legal/tester/"

    # 3) 문의 링크
    contact_url = cfg.get("contact_url") or getattr(
        settings, "LEGAL_CONTACT_URL", os.environ.get("LEGAL_CONTACT_URL")
    )
    if not contact_url:
        email = cfg.get("contact_email") or ""
        contact_url = f"mailto:{email}" if email else "#"
    cfg["contact_link"] = contact_url

    # 4) ✅ ToS 버전/시행일 키를 "항상 존재"하게 만들기
    tos_ver = (
        cfg.get("tos_version")
        or cfg.get("terms_version")
        or os.environ.get("TOS_VERSION")
        or os.environ.get("TERMS_VERSION")
        or getattr(settings, "TOS_VERSION", None)
        or getattr(settings, "TERMS_VERSION", None)
        or "v1.0"
    )
    tos_eff = (
        cfg.get("tos_effective_date")
        or cfg.get("terms_effective_date")
        or cfg.get("effective_date")
        or os.environ.get("TOS_EFFECTIVE_DATE")
        or os.environ.get("TERMS_EFFECTIVE_DATE")
        or getattr(settings, "TOS_EFFECTIVE_DATE", None)
        or getattr(settings, "TERMS_EFFECTIVE_DATE", None)
        or timezone.localdate()
    )

    cfg["terms_version"] = str(tos_ver).strip() if tos_ver else "v1.0"
    cfg["tos_version"] = cfg["terms_version"]

    cfg["terms_effective_date"] = _to_datestr(tos_eff)
    cfg["tos_effective_date"] = cfg["terms_effective_date"]

    # ✅ 템플릿이 레거시로 cfg.effective_date를 참조해도 절대 안 터짐
    cfg["effective_date"] = cfg["terms_effective_date"]

    # 4-1) ✅ Tester 버전/시행일도 "항상 존재"하게 만들기
    tester_ver = (
        cfg.get("tester_version")
        or os.environ.get("TESTER_VERSION")
        or getattr(settings, "TESTER_VERSION", None)
        or "v1.0"
    )
    tester_eff = (
        cfg.get("tester_effective_date")
        or os.environ.get("TESTER_EFFECTIVE_DATE")
        or getattr(settings, "TESTER_EFFECTIVE_DATE", None)
        or cfg.get("terms_effective_date")
        or timezone.localdate()
    )
    cfg["tester_version"] = str(tester_ver).strip() if tester_ver else "v1.0"
    cfg["tester_effective_date"] = _to_datestr(tester_eff)

    # 5) ✅ 국외이전 관련 원칙 문구 기본값(빈 bullet 방지)  ✅ 빈 문자열이면 덮어쓰기
    if not str(cfg.get("policy_minimization") or "").strip():
        cfg["policy_minimization"] = "국외 이전은 서비스 제공에 필요한 최소 범위의 정보에 한해 수행합니다."
    if not str(cfg.get("policy_processing_location") or "").strip():
        cfg["policy_processing_location"] = "전송은 TLS/HTTPS로 암호화되며, 접근 통제·권한 관리·보안 모니터링을 적용합니다."
    if not str(cfg.get("policy_change_note") or "").strip():
        cfg["policy_change_note"] = "보관기간 경과 또는 목적 달성 시 지체 없이 파기하며, 동의 거부 시 관련 기능 이용이 제한될 수 있습니다."

    cfg["overseas_principles"] = [
        cfg["policy_minimization"],
        cfg["policy_processing_location"],
        cfg["policy_change_note"],
    ]

    # 6) 처리위탁(수탁자) 표 rows
    try:
        retention_default = int(cfg.get("retention_days") or 90)
        processors = legal_tables.build_processors(
            retention_days_default=retention_default,
            contact_email=cfg.get("contact_email", ""),
        )
    except Exception:
        processors = []
    cfg.setdefault("processors", processors)

    # 7) 국외이전 표 rows
    try:
        retention_default_chatlog = getattr(
            settings,
            "RETENTION_DAYS_CHATLOG",
            getattr(settings, "RETENTION_DAYS", int(cfg.get("retention_days") or 90)),
        )
        vertex_location = getattr(
            settings,
            "VERTEX_LOCATION",
            getattr(settings, "GCP_LOCATION", ""),
        ) or "us-central1"

        overseas_rows = legal_tables.build_overseas_transfers(
            retention_days_default=int(retention_default_chatlog),
            contact_email=cfg.get("contact_email", ""),
            vertex_location=vertex_location,
        )
    except Exception:
        overseas_rows = []
    cfg.setdefault("overseas_transfers", overseas_rows)

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
    """
    inst, _ = _get_active_cfg_obj()
    raw_html = ""
    if inst and hasattr(inst, slot_key_in_db):
        raw_html = getattr(inst, slot_key_in_db) or ""
    if not raw_html:
        raw_html = getattr(settings, slot_key_in_settings, os.environ.get(slot_key_in_settings, ""))

    base_cfg, last_updated = _build_cfg_dict()

    ctx: Dict[str, Any] = {
        "cfg": base_cfg,
        "last_updated": last_updated,
        "raw_html": raw_html,
        "overseas_principles": base_cfg.get("overseas_principles", []),
        # 하위호환: 기존 키 유지
        slot_key_in_db: raw_html,
        slot_key_in_settings: raw_html,
    }
    if extra_ctx:
        ctx.update(extra_ctx)

    try:
        tpl = select_template(template_candidates)
        return HttpResponse(tpl.render(ctx, request))
    except TemplateDoesNotExist:
        body = raw_html or "<h1>문서 템플릿이 없습니다.</h1>"
        return HttpResponse(body)


# ------------------------------
# Pages
# ------------------------------
def legal_privacy(request: HttpRequest) -> HttpResponse:
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
            # ✅ tos/terms 둘 다 잡아주기 (네가 예전에 terms.html로 썼어도 안 깨짐)
            "legal/tos.html",
            "legal/terms.html",
            "ragapp/legal/tos.html",
            "ragapp/legal/terms.html",
        ],
        slot_key_in_db="tos_html",
        slot_key_in_settings="LEGAL_TOS_HTML",
    )


def legal_overseas(request: HttpRequest) -> HttpResponse:
    contact_email = _get_contact_email()
    retention_default = getattr(
        settings,
        "RETENTION_DAYS_CHATLOG",
        getattr(settings, "RETENTION_DAYS", 90),
    )
    vertex_location = getattr(
        settings,
        "VERTEX_LOCATION",
        getattr(settings, "GCP_LOCATION", ""),
    ) or "us-central1"

    try:
        overseas_rows = legal_tables.build_overseas_transfers(
            retention_days_default=retention_default,
            contact_email=contact_email,
            vertex_location=vertex_location,
        )
    except Exception:
        overseas_rows = []

    return _render_slot_page(
        request,
        template_candidates=[
            "legal/overseas.html",
            "ragapp/legal/overseas.html",
        ],
        slot_key_in_db="overseas_html",
        slot_key_in_settings="LEGAL_OVERSEAS_HTML",
        extra_ctx={
            "overseas_rows": overseas_rows,
        },
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
    base_cfg, last_updated = _build_cfg_dict()

    # 국외이전 rows 재사용
    contact_email = _get_contact_email()
    retention_default = getattr(
        settings,
        "RETENTION_DAYS_CHATLOG",
        getattr(settings, "RETENTION_DAYS", 90),
    )
    vertex_location = getattr(
        settings,
        "VERTEX_LOCATION",
        getattr(settings, "GCP_LOCATION", ""),
    ) or "us-central1"
    try:
        overseas_rows = legal_tables.build_overseas_transfers(
            retention_days_default=retention_default,
            contact_email=contact_email,
            vertex_location=vertex_location,
        )
    except Exception:
        overseas_rows = []

    def _safe_html(
        tpl_name: str,
        db_key: str,
        settings_key: str,
        extra_ctx: Optional[Dict[str, Any]] = None,
    ) -> str:
        try:
            tpl = select_template([tpl_name, f"ragapp/{tpl_name}"])
            ctx: Dict[str, Any] = {
                "cfg": base_cfg,
                "last_updated": last_updated,
                "raw_html": "",
                "overseas_principles": base_cfg.get("overseas_principles", []),
            }
            if extra_ctx:
                ctx.update(extra_ctx)
            return tpl.render(ctx)
        except Exception:
            pass

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
            "overseas_html": _safe_html(
                "legal/overseas.html",
                "overseas_html",
                "LEGAL_OVERSEAS_HTML",
                extra_ctx={"overseas_rows": overseas_rows},
            ),
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
