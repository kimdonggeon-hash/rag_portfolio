# ragapp/services/legal_tables.py
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from django.conf import settings


# -----------------------------
# Env(JSON) override helpers
# -----------------------------
def _env_json_list(name: str) -> Optional[List[Dict[str, Any]]]:
    """
    settings 또는 환경변수에 JSON 문자열로 넣으면 그대로 사용:
      LEGAL_PROCESSORS_JSON='[{"vendor":"...", "task":"..."}]'
      LEGAL_OVERSEAS_TRANSFERS_JSON='[{"recipient":"...", "country":"..."}]'
    """
    raw = (getattr(settings, name, None) or "").strip()
    if not raw:
        raw = (os.environ.get(name, "") or "").strip()
    if not raw:
        return None

    try:
        v = json.loads(raw)
    except Exception:
        return None

    if not isinstance(v, list):
        return None

    out: List[Dict[str, Any]] = []
    for row in v:
        if isinstance(row, dict):
            out.append(row)
    return out or None


# -----------------------------
# Runtime detectors (best-effort)
# -----------------------------
def _cloud_run_region_default() -> str:
    # Cloud Run에서 흔히 잡히는 환경변수들(환경마다 다를 수 있어 fallback 다 넣음)
    return (
        (os.environ.get("GOOGLE_CLOUD_REGION") or "").strip()
        or (os.environ.get("CLOUD_RUN_REGION") or "").strip()
        or (os.environ.get("K_REGION") or "").strip()
        or (getattr(settings, "CLOUD_RUN_REGION", "") or "").strip()
        or "asia-northeast1"  # 너 프로젝트 기본(도쿄)
    )


def _region_to_country_ko(region: str) -> str:
    r = (region or "").strip().lower()
    # 너가 쓰는 범위 + 흔한 것들만 “명확히” 매핑
    if r == "asia-northeast1":
        return "일본"
    if r == "asia-northeast3":
        return "대한민국"
    if r.startswith("asia-northeast"):
        # 1=일본, 2=홍콩, 3=한국(일반적으로 알려진 매핑이지만 환경/정책상 변동 가능)
        # 여기서는 과단정 피하고 “해당 리전이 위치한 국가”로 둔다.
        return "해당 리전이 위치한 국가"
    if r.startswith("us-"):
        return "미국"
    if r.startswith("europe-"):
        return "유럽연합/유럽(리전별 상이)"
    return "해당 리전이 위치한 국가"


def _is_postgres_cloudsql() -> bool:
    try:
        db = settings.DATABASES.get("default", {})  # type: ignore[attr-defined]
        eng = str(db.get("ENGINE", "")).lower()
        host = str(db.get("HOST", "")).lower()
        return ("postgres" in eng) and ("/cloudsql/" in host or "cloudsql" in host)
    except Exception:
        return False


def _uses_vertex_ai() -> bool:
    # 실제 사용 여부를 100% 판별하긴 어렵지만,
    # Vertex 프로젝트가 잡혀 있으면 “사용 가능/사용 중”으로 보는 보수적(투명) 기준
    try:
        pid = str(getattr(settings, "VERTEX_PROJECT_ID", "") or "").strip()
        return bool(pid)
    except Exception:
        return False


def _uses_cloud_storage() -> bool:
    # 너 settings에서 USE_GCS_UPLOADS + GS_BUCKET_NAME 조합이 있으므로 그 기준을 우선
    try:
        use = bool(getattr(settings, "USE_GCS_UPLOADS", False))
        bucket = str(getattr(settings, "GS_BUCKET_NAME", "") or "").strip()
        return bool(use and bucket)
    except Exception:
        return False


# -----------------------------
# Builders
# -----------------------------
def build_processors(*, retention_days_default: int, contact_email: str) -> List[Dict[str, str]]:
    """
    개인정보처리방침의 “처리위탁(수탁자)” 표 rows

    템플릿 키:
      - vendor, task, items, retention
    """
    env_rows = _env_json_list("LEGAL_PROCESSORS_JSON")
    if env_rows:
        norm: List[Dict[str, str]] = []
        for r in env_rows:
            norm.append(
                {
                    "vendor": str(r.get("vendor", "") or "").strip(),
                    "task": str(r.get("task", "") or "").strip(),
                    "items": str(r.get("items", "") or "").strip(),
                    "retention": str(r.get("retention", "") or "").strip(),
                }
            )
        return [r for r in norm if r["vendor"] and r["task"]]

    rows: List[Dict[str, str]] = []

    # 공통 보관 문구(너 프로젝트 정책: “항목별 보관정책 + 기본 보유기간”)
    default_ret = f"항목별 보관정책에 따름(기본 {retention_days_default}일 등) — 목적 달성/기간 경과 시 파기"

    # 1) 클라우드 인프라(Cloud Run)
    rows.append(
        {
            "vendor": "Google LLC (Google Cloud Platform)",
            "task": "클라우드 인프라 제공(Cloud Run): 웹 서비스 실행/확장/네트워크 제공",
            "items": (
                "서비스 이용 시 생성·전송되는 데이터(사용자 입력/업로드 데이터 포함) 및 "
                "운영에 필요한 최소 기술정보(요청 시각/경로/응답코드/오류정보/User-Agent 등)"
            ),
            "retention": default_ret,
        }
    )

    # 2) DB (Cloud SQL 사용 시)
    if _is_postgres_cloudsql():
        rows.append(
            {
                "vendor": "Google LLC (Google Cloud Platform)",
                "task": "데이터베이스 제공(Cloud SQL: PostgreSQL): 서비스 데이터 저장/조회",
                "items": (
                    "서비스 운영을 위해 저장되는 데이터(예: 계정/설정, 질문·피드백·상담 세션 메타데이터, "
                    "이용 로그 등 서비스 기능 수행에 필요한 범위)"
                ),
                "retention": default_ret,
            }
        )

    # 3) 로그/모니터링
    rows.append(
        {
            "vendor": "Google LLC (Google Cloud Platform)",
            "task": "로그/모니터링 제공(Cloud Logging/Monitoring): 장애 분석/보안 모니터링",
            "items": (
                "운영/오류 로그(가능한 범위에서 최소화·가명처리/해시 처리 적용), "
                "보안 이벤트·성능 지표 등 서비스 안정화에 필요한 기술 정보"
            ),
            "retention": default_ret,
        }
    )

    # 4) 파일 저장(GCS 업로드를 켠 경우)
    if _uses_cloud_storage():
        rows.append(
            {
                "vendor": "Google LLC (Google Cloud Platform)",
                "task": "파일 저장(Cloud Storage): 업로드 파일 저장/처리",
                "items": "업로드 파일(PDF/이미지/표 등) 및 처리 과정에서 생성되는 임시 데이터",
                "retention": (
                    "설정된 수명주기(Lifecycle) 정책에 따름(원칙: 목적 달성 시 삭제, "
                    "안정화 목적의 단기 보관을 허용하더라도 최소 기간으로 운영)"
                ),
            }
        )

    # 5) AI 처리(Vertex AI/Gemini) 사용 시
    if _uses_vertex_ai():
        rows.append(
            {
                "vendor": "Google LLC (Google Cloud Platform / Vertex AI, Gemini)",
                "task": "AI 처리(Vertex AI/Gemini): 답변 생성·요약·분석(요청 시)",
                "items": (
                    "사용자 입력 텍스트 및 업로드 파일에서 추출된 텍스트(요청 처리에 필요한 범위), "
                    "생성 결과(서비스 설정에 따라 저장/비저장), 최소 기술 로그"
                ),
                "retention": (
                    f"서비스 내부 저장 데이터는 {retention_days_default}일 등 보관정책에 따라 최소기간 보관 후 삭제. "
                    "공급자 측 처리·보관 방식은 공급자 정책(약관/개인정보처리방침)에 따름"
                ),
            }
        )

    # 문의 문구는 표 밖(템플릿의 문의 영역)에 두는 게 가장 깔끔하지만,
    # 템플릿 구조상 필요하면 rows 외부에서 contact_email을 별도로 출력하면 됨.
    _ = contact_email  # unused 방지(가독성)

    return rows


def build_overseas_transfers(
    *,
    retention_days_default: int,
    contact_email: str,
    vertex_location: str,
) -> List[Dict[str, str]]:
    """
    /legal/overseas/ “국외이전” 표 rows

    템플릿 키:
      - recipient, country, timing_method, items, purpose, retention, refusal
    """
    env_rows = _env_json_list("LEGAL_OVERSEAS_TRANSFERS_JSON")
    if env_rows:
        norm: List[Dict[str, str]] = []
        for r in env_rows:
            norm.append(
                {
                    "recipient": str(r.get("recipient", "") or "").strip(),
                    "country": str(r.get("country", "") or "").strip(),
                    "timing_method": str(r.get("timing_method", "") or "").strip(),
                    "items": str(r.get("items", "") or "").strip(),
                    "purpose": str(r.get("purpose", "") or "").strip(),
                    "retention": str(r.get("retention", "") or "").strip(),
                    "refusal": str(r.get("refusal", "") or "").strip(),
                }
            )
        return [r for r in norm if r["recipient"] and r["country"]]

    # 공통 “거부/문의” 문구 (과장 없이, 권리 + 제한 가능성만 명시)
    if contact_email:
        common_refusal = (
            "이용자는 개인정보의 국외 이전에 대한 동의를 거부할 권리가 있습니다. "
            f"다만 국외 이전이 필요한 기능 이용이 제한될 수 있습니다. 문의: {contact_email}"
        )
        ai_refusal = (
            "이용자는 AI 기능 제공을 위한 개인정보의 국외 이전에 동의하지 않을 권리가 있습니다. "
            f"동의하지 않는 경우 AI 관련 기능 이용이 제한될 수 있습니다. 문의: {contact_email}"
        )
    else:
        common_refusal = (
            "이용자는 개인정보의 국외 이전에 대한 동의를 거부할 권리가 있습니다. "
            "다만 국외 이전이 필요한 기능 이용이 제한될 수 있습니다."
        )
        ai_refusal = (
            "이용자는 AI 기능 제공을 위한 개인정보의 국외 이전에 동의하지 않을 권리가 있습니다. "
            "동의하지 않는 경우 AI 관련 기능 이용이 제한될 수 있습니다."
        )

    rows: List[Dict[str, str]] = []

    # 너 프로젝트: “전부 도쿄 통일” 기준으로, 실제 리전 값을 최대한 표시
    cloud_region = _cloud_run_region_default()
    cloud_country = _region_to_country_ko(cloud_region)

    # 1) 서비스 인프라(GCP)
    rows.append(
        {
            "recipient": "Google LLC (Google Cloud Platform)",
            "country": f"{cloud_country} (리전: {cloud_region})",
            "timing_method": "이용자가 서비스를 사용할 때마다 인터넷망을 통해 TLS/HTTPS 방식으로 암호화 전송",
            "items": (
                "회원 기능 이용 시 계정 정보(예: 이메일, 비밀번호는 암호화되어 저장), "
                "사용자가 입력한 내용(게시글·댓글·상담 내용·질문 등), "
                "업로드 파일(PDF·이미지·표 등) 및 처리 과정의 임시 데이터, "
                "서비스 운영에 필요한 최소 기술 로그(요청 시각/경로/응답코드/오류정보/User-Agent, "
                "설정에 따라 해시 처리된 IP 등)"
            ),
            "purpose": "웹 서비스 제공, 데이터 저장/조회, 장애 대응, 보안 모니터링 및 서비스 안정화",
            "retention": (
                f"서비스 내부 보관 데이터는 항목별 보관정책에 따라 최소기간 보관(기본 {retention_days_default}일 등) 후 파기. "
                "백업·복구 등 운영상 필요한 범위에서 처리될 수 있음"
            ),
            "refusal": common_refusal,
        }
    )

    # 2) AI 기능(Vertex AI/Gemini) — 너는 도쿄 통일 예정이므로 동일 리전에 정리
    if _uses_vertex_ai():
        loc = (vertex_location or "").strip() or cloud_region
        loc_country = _region_to_country_ko(loc)
        rows.append(
            {
                "recipient": "Google LLC (Google Cloud Platform / Vertex AI, Gemini)",
                "country": f"{loc_country} (리전: {loc})",
                "timing_method": "이용자가 AI 질문/요약/분석 요청을 보낼 때마다 인터넷망을 통해 TLS/HTTPS 방식으로 암호화 전송",
                "items": (
                    "이용자가 입력한 질문·대화 내용, 요약·분석 대상 텍스트(필요 범위), "
                    "세션 식별자 등 서비스 내부 식별 정보(가능한 범위에서 최소화), "
                    "오류 분석을 위한 최소 기술 로그"
                ),
                "purpose": "AI 답변 생성/요약/분석 등 기능 제공 및 안정성 개선(오류 분석 포함)",
                "retention": (
                    f"서비스 내부 저장 데이터는 항목별 보관정책에 따라 최소기간 보관(기본 {retention_days_default}일 등) 후 삭제. "
                    "공급자 측 처리·보관 방식은 공급자 정책(약관/개인정보처리방침)에 따름"
                ),
                "refusal": ai_refusal,
            }
        )

    return rows
