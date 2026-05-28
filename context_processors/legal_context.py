# ragapp/context_processors/legal_context.py
from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from ragapp.services.legal_tables import build_processors, build_overseas_transfers


def _as_int(v, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return default


def legal_context(request):
    # ✅ 템플릿/법적 문서에서 공통으로 쓰는 cfg
    contact_email = (getattr(settings, "CONTACT_EMAIL", "") or "").strip() or "kdg283@gmail.com"
    retention = _as_int(getattr(settings, "RETENTION_DAYS_DEFAULT", None) or getattr(settings, "RETENTION_DAYS", 30), 30)

    # ✅ 너 settings.py 기준: GEMINI_TEXT_MODEL / VERTEX_TEXT_MODEL / GEMINI_MODEL 중 실제 존재하는 걸로 모델명 표시
    model_name = (
        getattr(settings, "GEMINI_TEXT_MODEL", None)
        or getattr(settings, "VERTEX_TEXT_MODEL", None)
        or getattr(settings, "GEMINI_MODEL", None)
        or "gemini"
    )

    vertex_location = str(getattr(settings, "VERTEX_LOCATION", "us-central1") or "us-central1")

    cfg = {
        # 기본 정보
        "service_name": getattr(settings, "SERVICE_NAME", "김동건의 포트폴리오"),
        "operator_name": getattr(settings, "OPERATOR_NAME", "김동건"),
        "contact_email": contact_email,
        "retention_days": retention,

        # 문서 메타 (overseas.html 상단에서 표시)
        "legal_docs_version": getattr(settings, "LEGAL_DOCS_VERSION", "v1.0"),
        "legal_effective_date": getattr(settings, "LEGAL_EFFECTIVE_DATE", ""),

        # 안전/컴플라이언스 토글(표현은 “사실”만)
        "model_name": model_name,
        "summary_only": bool(getattr(settings, "SAFE_SUMMARY_ONLY", True)),
        "store_fulltext": bool(getattr(settings, "STORE_FULLTEXT", False)),
        "robots_on": bool(getattr(settings, "RESPECT_ROBOTS", True)),
        "safe_mode_enabled": bool(getattr(settings, "SAFE_MODE_ENABLED", True)),
        "log_ip_hashed": bool(getattr(settings, "LOG_IP_HASHED", True)),

        # 원칙 문구(템플릿에서 default로 fallback 되지만, cfg에 있으면 그걸 우선)
        "policy_minimization": getattr(
            settings,
            "POLICY_MINIMIZATION",
            "국외 이전은 서비스 제공에 필요한 최소 범위의 정보에 한해 수행합니다.",
        ),
        "policy_processing_location": getattr(
            settings,
            "POLICY_PROCESSING_LOCATION",
            "전송은 TLS/HTTPS로 암호화되며, 접근 통제·권한 관리·보안 모니터링을 적용합니다.",
        ),
        "policy_change_note": getattr(
            settings,
            "POLICY_CHANGE_NOTE",
            "보관기간 경과 또는 목적 달성 시 지체 없이 파기하며, 동의 거부 시 관련 기능 이용이 제한될 수 있습니다.",
        ),
    }

    # ✅ 표 rows 생성 (문서에 표시되는 내용이라서, 실패 시에도 페이지는 뜨게 “보수적” 처리)
    try:
        cfg["processors"] = build_processors(
            retention_days_default=retention,
            contact_email=contact_email,
        )
    except Exception:
        cfg["processors"] = []

    try:
        cfg["overseas_transfers"] = build_overseas_transfers(
            retention_days_default=retention,
            contact_email=contact_email,
            vertex_location=vertex_location,
        )
    except Exception:
        cfg["overseas_transfers"] = []

    return {
        "cfg": cfg,
        "last_updated": timezone.now(),
    }
