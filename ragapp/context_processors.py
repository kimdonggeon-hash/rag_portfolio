# ragapp/context_processors.py
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List
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
        "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),
        "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
    }


def static_version(_request) -> Dict[str, Any]:
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


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    if v is None:
        return default
    s = str(v).strip()
    return s if s else default


def _bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in ("1", "true", "t", "yes", "y", "on")


def _is_cloud_run() -> bool:
    return bool(
        os.environ.get("K_SERVICE")
        or os.environ.get("K_REVISION")
        or os.environ.get("CLOUD_RUN_JOB")
        or os.environ.get("CLOUD_RUN_JOB_NAME")
    )


def _fmt(s: Any, **kwargs) -> str:
    txt = "" if s is None else str(s)
    try:
        return txt.format(**kwargs)
    except Exception:
        return txt


def _build_processors(retention_days: int, log_retention_days: int, chat_retention_days: int) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []

    db_host = (_env("DB_HOST") or "").strip()
    has_cloudsql = ("/cloudsql/" in db_host) or bool(_env("INSTANCE_CONNECTION_NAME"))

    gs_bucket = (_env("GS_BUCKET_NAME") or "").strip()
    default_file_storage = str(getattr(settings, "DEFAULT_FILE_STORAGE", "") or "")
    storages_setting = getattr(settings, "STORAGES", {}) or {}
    storages_default_backend = ""
    try:
        storages_default_backend = str((storages_setting.get("default") or {}).get("BACKEND") or "")
    except Exception:
        storages_default_backend = ""

    using_gcs = bool(gs_bucket) or ("gcloud" in default_file_storage.lower()) or ("storages" in storages_default_backend.lower())

    use_vertex = _bool(_env("GOOGLE_GENAI_USE_VERTEXAI", str(getattr(settings, "GOOGLE_GENAI_USE_VERTEXAI", "true"))), default=True)
    vertex_location = str(getattr(settings, "VERTEX_LOCATION", _env("VERTEX_LOCATION", "")) or "")
    zdr = _bool(getattr(settings, "VERTEX_ZERO_DATA_RETENTION", False), default=False)

    upload_retention = "처리 완료 후 즉시 삭제(또는 최대 24시간 내 삭제)"

    rows.append({
        "vendor": "Google Cloud (Cloud Run)",
        "task": "웹 서비스 실행/오토스케일/네트워크 처리",
        "items": "요청 메타데이터(접속 시각, 경로, 응답 코드 등), 서비스 운영/보안 목적 로그(설정에 따라 최소화)",
        "retention": f"운영 정책에 따라 보관 후 파기(예: 로그 기본 {log_retention_days}일 등)",
    })

    if has_cloudsql:
        rows.append({
            "vendor": "Google Cloud (Cloud SQL - PostgreSQL)",
            "task": "서비스 데이터베이스 운영(저장/조회/백업 등)",
            "items": "서비스 이용 중 생성되는 DB 저장 데이터(질문/피드백/상담 기록/운영 로그 등) — 저장 범위는 서비스 설정 및 이용 흐름에 따름",
            "retention": f"서비스 정책에 따라 보관 후 파기(예: 채팅 기본 {chat_retention_days}일, 로그 기본 {log_retention_days}일 등)",
        })

    if use_vertex:
        loc_txt = f" / 리전: {vertex_location}" if vertex_location else ""
        zdr_txt = " (Zero Data Retention 옵션 사용)" if zdr else ""
        rows.append({
            "vendor": f"Google Cloud (Vertex AI){loc_txt}",
            "task": "AI 추론/요약/임베딩 생성",
            "items": "질문/검색어/프롬프트, 모델에 전달되는 컨텍스트(요약/발췌 텍스트), 모델 응답(요약/답변) — 최소 범위로 전송",
            "retention": f"요청 처리 목적 범위 내에서 처리{zdr_txt} (공급자 정책 및 설정에 따름)",
        })

    if using_gcs:
        bucket_txt = f" (버킷: {gs_bucket})" if gs_bucket else ""
        rows.append({
            "vendor": f"Google Cloud (Cloud Storage){bucket_txt}",
            "task": "업로드 파일/객체 저장(선택 구성)",
            "items": "업로드된 PDF/이미지/표 파일(처리 중 임시 저장 가능), 사용자가 ‘저장’을 선택한 결과물(내 자료 검색 DB 등)",
            "retention": f"업로드 파일: {upload_retention} / 저장 항목: 사용자가 삭제하기 전까지",
        })

    rows.append({
        "vendor": "Google Cloud (Cloud Logging/Monitoring)",
        "task": "오류/장애 분석, 보안 모니터링, 운영 로그 수집",
        "items": "요청/응답 로그, 오류 스택, 성능 메트릭 등(개인정보 최소화 설정 적용 가능)",
        "retention": f"로그 버킷 보관 설정에 따름(예: 기본 {log_retention_days}일 등)",
    })

    return rows


def legal_context(request) -> Dict[str, Any]:
    cfg_model = None
    if LegalConfig is not None:
        try:
            cfg_model = LegalConfig.get_solo()
        except Exception:
            cfg_model = None

    service_name = (getattr(cfg_model, "service_name", None) or "AI 뉴스 분석 콘솔")
    effective_date = getattr(cfg_model, "effective_date", None) or date.today()
    operator_name = (getattr(cfg_model, "operator_name", None) or "운영자")
    contact_email = (getattr(cfg_model, "contact_email", None) or "kdg283@gmail.com")
    contact_phone = getattr(cfg_model, "contact_phone", "") or ""
    contact_link = getattr(cfg_model, "contact_link", "") or ""

    consent_gate_enabled = bool(getattr(cfg_model, "consent_gate_enabled", True))

    privacy_html = (
        getattr(cfg_model, "sanitized_privacy_html", "")
        or getattr(cfg_model, "privacy_html", "")
        or ""
    )
    cross_border_html = (
        getattr(cfg_model, "sanitized_cross_border_html", "")
        or getattr(cfg_model, "cross_border_html", "")
        or ""
    )
    tester_html = (
        getattr(cfg_model, "sanitized_tester_html", "")
        or getattr(cfg_model, "tester_html", "")
        or ""
    )

    require_checkbox = True
    require_modal = False
    privacy_page_url = _get_privacy_url()

    retention_days = int(getattr(settings, "RETENTION_DAYS", 30) or 30)
    log_retention_days = int(getattr(settings, "LOG_RETENTION_DAYS", retention_days) or retention_days)
    chat_retention_days = int(getattr(settings, "CHAT_RETENTION_DAYS", retention_days) or retention_days)

    processors = _build_processors(retention_days, log_retention_days, chat_retention_days)

    # ✅ DB에서 즉시 조절되는 “정책 문장”
    policy_minimization = getattr(cfg_model, "policy_minimization", "") or ""
    policy_processing_location = getattr(cfg_model, "policy_processing_location", "") or ""
    policy_change_note = getattr(cfg_model, "policy_change_note", "") or ""

    if not policy_minimization:
        policy_minimization = "본 서비스는 개인정보 최소 수집을 원칙으로 하며, 이용자가 개인정보를 입력·업로드하지 않도록 안내합니다. 원문 저장은 최소화하고 가능하면 요약/메타데이터 중심으로 처리합니다."
    if not policy_processing_location:
        policy_processing_location = "본 서비스는 클라우드 인프라 및 AI API를 활용하여 처리하며, 처리 위치(리전/국가)는 공급자 정책 및 운영 상황에 따라 변동될 수 있습니다. 중요 변경사항은 본 문서를 통해 갱신 고지합니다."
    if not policy_change_note:
        policy_change_note = "기술적 구성(저장 방식, 처리 흐름, 로그 항목 등)은 보안/안정성/비용 최적화를 위해 변경될 수 있으며, 변경 시 개인정보 보호 원칙(최소 수집, 목적 제한, 보관기간 준수)을 우선합니다."

    # ✅ 국외이전 표: DB JSON → 없으면 안전 기본 1행(하지만 템플릿은 하드코딩 0)
    raw_transfers = getattr(cfg_model, "overseas_transfers", None)
    overseas_transfers: List[Dict[str, Any]] = []
    if isinstance(raw_transfers, list):
        overseas_transfers = [r for r in raw_transfers if isinstance(r, dict)]

    if not overseas_transfers:
        overseas_transfers = [{
            "recipient": "Google Cloud (Cloud Run / Cloud SQL / Vertex AI)",
            "country": "국외(데이터센터 위치는 공급자 정책 및 운영 상황에 따름)",
            "timing_method": "서비스 이용 시 네트워크를 통해 전송·처리(필요 시 저장)",
            "items": "서비스 입력/업로드 데이터 및 운영에 필요한 최소 정보",
            "purpose": "기능 제공(검색/요약/RAG), 오류·장애 분석, 보안 모니터링",
            "retention": "서비스 정책 및 공급자 정책/설정에 따름(예: 기본 {retention_days}일)",
            "refusal": "국외이전 관련 문의: {contact_email}",
        }]

    # placeholder 치환
    for r in overseas_transfers:
        r["retention"] = _fmt(r.get("retention", ""), retention_days=retention_days, contact_email=contact_email)
        r["refusal"] = _fmt(r.get("refusal", ""), retention_days=retention_days, contact_email=contact_email)

    # ✅ 약관/테스터 헤더(버전/시행일)도 DB로 조절
    tos_version = getattr(cfg_model, "tos_version", "v1.0") or "v1.0"
    tos_effective_date = getattr(cfg_model, "tos_effective_date", None) or effective_date

    tester_version = getattr(cfg_model, "tester_version", "v1.0") or "v1.0"
    tester_effective_date = getattr(cfg_model, "tester_effective_date", None) or effective_date

    flat: Dict[str, Any] = {
        "service_name": service_name,
        "effective_date": effective_date,
        "operator_name": operator_name,
        "contact_email": contact_email,
        "contact_phone": contact_phone,
        "consent_gate_enabled": consent_gate_enabled,
        "PRIVACY_PAGE_URL": privacy_page_url,
        "privacy_html": privacy_html,
        "cross_border_html": cross_border_html,
        "tester_html": tester_html,
        "require_checkbox": require_checkbox,
        "require_modal": require_modal,
    }

    flat["LEGAL"] = {
        "service_name": service_name,
        "effective_date": effective_date.isoformat() if hasattr(effective_date, "isoformat") else str(effective_date),
        "operator_name": operator_name,
        "contact_email": contact_email,
        "contact_phone": contact_phone,
        "consent_gate_enabled": consent_gate_enabled,
        "privacy_page_url": privacy_page_url,
        "require_checkbox": require_checkbox,
        "require_modal": require_modal,
    }

    flat["cfg"] = {
        "service_name": service_name,
        "operator_name": operator_name,
        "contact_email": contact_email,
        "contact_phone": contact_phone,
        "contact_link": contact_link,
        "consent_gate_enabled": consent_gate_enabled,

        "retention_days": retention_days,
        "log_retention_days": log_retention_days,
        "chat_retention_days": chat_retention_days,

        "processors": processors,

        "policy_minimization": policy_minimization,
        "policy_processing_location": policy_processing_location,
        "policy_change_note": policy_change_note,

        "overseas_transfers": overseas_transfers,

        "tos_version": tos_version,
        "tos_effective_date": tos_effective_date,
        "tester_version": tester_version,
        "tester_effective_date": tester_effective_date,

        "is_cloud_run": _is_cloud_run(),
    }

    flat["last_updated"] = timezone.now()
    return flat
