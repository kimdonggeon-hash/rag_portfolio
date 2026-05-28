# ragapp/machine/feature_config.py
from __future__ import annotations

import os
from pathlib import Path
from django.conf import settings
from django.utils import timezone

# ────────────────────────────────────────────────
# 환경 스위치
# ────────────────────────────────────────────────
PUBLIC_ALLOW_UPLOAD_IMAGES = (
    os.environ.get("PUBLIC_ALLOW_UPLOAD_IMAGES", "1").lower()
    not in ("0", "false", "no")
)
PUBLIC_ALLOW_UPLOAD_CSV = (
    os.environ.get("PUBLIC_ALLOW_UPLOAD_CSV", "1").lower()
    not in ("0", "false", "no")
)
PUBLIC_MAX_FILES = int(os.environ.get("PUBLIC_MAX_FILES", "10"))
PUBLIC_MAX_FILE_MB = int(os.environ.get("PUBLIC_MAX_FILE_MB", "15"))
PUBLIC_MAX_CSV_ROWS = int(os.environ.get("PUBLIC_MAX_CSV_ROWS", "1000"))

CHROMA_MEDIA_DIR = os.environ.get("CHROMA_MEDIA_DIR", "chroma_media")

# ✅ settings.py 에서 MEDIA_ROOT=/.../uploads, MEDIA_URL=/uploads/ 를 쓰는 걸 권장
MEDIA_ROOT = Path(
    getattr(settings, "MEDIA_ROOT", Path(settings.BASE_DIR) / "uploads")
).resolve()
MEDIA_URL = getattr(settings, "MEDIA_URL", "/uploads/")

try:
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

# 표 원본 데이터를 JSON 으로 보관할 디렉터리
TABLE_DATA_DIR = MEDIA_ROOT / "table_data"
try:
    TABLE_DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass


def _int(v, default):
    try:
        return int(v)
    except Exception:
        return default


def _tmp_upload_dir() -> Path:
    """
    임시 파일 저장 디렉터리.
    - Cloud Run/Linux: /tmp
    - Windows 로컬: TEMP/TMPDIR
    """
    base = (
        os.environ.get("TMPDIR")
        or os.environ.get("TEMP")
        or os.environ.get("TMP")
        or "/tmp"
    )
    p = Path(base).expanduser()
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    d = p / "ragapp_upload_tmp"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def truthy_env(name: str, default: str = "0") -> bool:
    v = (os.getenv(name) or default).strip().lower()
    return v in ("1", "true", "y", "yes", "on")
