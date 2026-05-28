# ragapp/services/gcs_signed_url.py

from __future__ import annotations

import logging
import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import google.auth
from google.auth.transport.requests import Request
from google.cloud import storage

log = logging.getLogger(__name__)


def _guess_bucket_name() -> str:
    bn = (
        os.environ.get("GS_BUCKET_NAME")
        or os.environ.get("GCS_BUCKET_NAME")
        or os.environ.get("GCS_BUCKET")
        or ""
    ).strip()
    if not bn:
        raise RuntimeError(
            "버킷 이름을 찾지 못했습니다. GS_BUCKET_NAME(또는 GCS_BUCKET_NAME)을 환경변수로 설정하세요."
        )
    return bn


def _normalize_object_name(path: str, bucket_name: str) -> str:
    """
    입력이 아래 형태들 중 무엇이든, 최종적으로 '버킷 내부 객체 경로'만 남깁니다.
    - uploads/images/..../a.jpg
    - /uploads/images/..../a.jpg
    - gs://bucket/uploads/images/..../a.jpg
    - https://storage.googleapis.com/bucket/uploads/images/..../a.jpg
    """
    p = (path or "").strip()
    if not p:
        return ""

    # gs://bucket/...
    if p.startswith("gs://"):
        u = urlparse(p)
        if u.netloc and u.netloc != bucket_name:
            return ""  # 다른 버킷이면 거절
        return u.path.lstrip("/")

    # https://storage.googleapis.com/bucket/object
    if p.startswith(("http://", "https://")):
        u = urlparse(p)
        parts = u.path.lstrip("/").split("/", 1)
        if len(parts) == 2 and parts[0] == bucket_name:
            return parts[1]
        return ""  # 버킷 매칭이 안 되면 서명 대상 아님

    # ✅ 일반 경로는 그대로 객체 키로
    return p.lstrip("/")


def _signing_sa_email(creds) -> str:
    sa = (os.getenv("GCS_SIGNING_SA") or "").strip()
    if sa:
        return sa

    sa2 = getattr(creds, "service_account_email", None)
    if isinstance(sa2, str) and sa2.strip():
        return sa2.strip()

    raise RuntimeError(
        "서명용 서비스계정 이메일을 못 찾았습니다. 환경변수 GCS_SIGNING_SA에 SA 이메일을 넣어주세요."
    )


def make_signed_url(
    path: str,
    *,
    expires_sec: int = 600,  # 10분
    bucket_name: str | None = None,
) -> str:
    """
    Cloud Run 기본 자격증명(토큰 기반)에서도 동작하도록
    IAM signBlob(google.auth.iam.Signer)로 v4 Signed URL을 생성합니다.

    실패하면 ""를 반환(호출 측에서 원본 URL로 fallback하도록 유도).
    """
    bucket = bucket_name or _guess_bucket_name()
    object_name = _normalize_object_name(path, bucket)

    # ✅ 빈 값 / traversal 방지
    if not object_name:
        return ""
    if ".." in Path(object_name).parts:
        return ""

    try:
        # ✅ expires_sec 안전 범위로 고정(1분~1시간)
        expires_sec = max(60, min(int(expires_sec), 3600))

        creds, project_id = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        sa_email = _signing_sa_email(creds)

        # ✅ 토큰이 None인 경우가 많아서 반드시 refresh
        creds.refresh(Request())
        access_token = getattr(creds, "token", None)
        if not access_token:
            return ""

        client = storage.Client(project=project_id, credentials=creds) if project_id else storage.Client(credentials=creds)
        blob = client.bucket(bucket).blob(object_name)

        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=expires_sec),
            method="GET",
            service_account_email=sa_email,
            access_token=access_token,
        )
    except Exception as e:
        log.warning("signed url failed: %r", e)
        return ""