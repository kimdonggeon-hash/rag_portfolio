# ragsite/settings.py
from __future__ import annotations

from pathlib import Path
import sys
import os
from typing import Any, Dict, List, Optional

# ─── BASE ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent

# ─── .env 로드 ───────────────────────────────────────────────────────────────
# ⚠️ override=True는 쓰지 않는다: scripts/run_local.ps1처럼 로컬 실행 스크립트가
# 의도적으로 DJANGO_DEBUG=1 / SECURE_SSL_REDIRECT=0 등을 .env보다 우선해서
# 세팅하는 경우가 있는데, override=True면 그 의도된 오버라이드까지 .env가
# 덮어써버려서(예: DEBUG가 도로 0이 되어 로컬에서 HTTPS 강제 리다이렉트 발생)
# 로컬 개발 워크플로가 깨진다. (한 번 override=True로 바꿨다가 이 문제로 되돌림.)
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(BASE_DIR / ".env")
except Exception:
    pass

# ─── 게시판 영역 ─────────────────────────────────────────────────────────────
BOARD_PATH_PREFIX = "/board/"

# 금칙어(옵션: 기본은 비워두고, 관리자에서 BoardAbuseKeyword로 관리 추천)
BOARD_BANNED_WORDS: List[str] = []
BOARD_BANNED_REGEX: List[str] = []

# ✅ Ban 누적/차단
BOARD_BAN_THRESHOLD = 3        # 누적 3점이면 하루 차단
BOARD_BAN_TTL_SEC = 86400      # 24시간
BOARD_BAN_POINTS_WORD = 1      # 단어 적발 1점
BOARD_BAN_POINTS_REGEX = 2     # 정규식 적발 2점(더 강하게)
BOARD_BAN_INDEX_TTL_SEC = 7 * 86400
BOARD_REPORT_HITS_INSTANT_BAN = 3
BOARD_REPORT_HITS_INSTANT_BAN_TTL_SEC = 86400  # 24h


# ─── 유틸: 환경변수 로딩 ────────────────────────────────────────────────────
def _dequote(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1].strip()
    return s


def _parse_csv(raw: str) -> List[str]:
    s = (raw or "").strip()
    if not s:
        return []
    return [x.strip() for x in s.split(",") if x.strip()]


def _env_first(keys: List[str], *, default: Optional[str] = None) -> Optional[str]:
    for k in keys:
        v = _dequote(os.environ.get(k))
        if v is not None and v != "":
            return v
    return default


def _env_bool(keys: List[str], *, default: bool = False) -> bool:
    v = _env_first(keys, default=None)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "t", "yes", "y", "on")


def _env_int(keys: List[str], *, default: int) -> int:
    v = _env_first(keys, default=None)
    if v is None:
        return default
    try:
        return int(str(v).strip())
    except Exception:
        return default


def _env_float(keys: List[str], *, default: float) -> float:
    v = _env_first(keys, default=None)
    if v is None:
        return default
    try:
        return float(str(v).strip())
    except Exception:
        return default


def _env_csv(keys: List[str], *, default: str = "") -> List[str]:
    raw = _env_first(keys, default=default) or ""
    return _parse_csv(raw)

def _rag_sources_where_from_env(default_csv: str) -> Dict[str, Any] | None:
    """
    RAG_SOURCES_FILTER env를 'source' where 조건으로 정규화.
    - env가 비어있거나 'all'이면 필터 없음(None)
    - 1개면 {"source": "upload_doc"}
    - 여러개면 {"source": {"$in": [...]} }
    """
    raw = (_env_first(["RAG_SOURCES_FILTER"], default=default_csv) or default_csv).strip()

    if not raw:
        return None

    low = raw.strip().lower()
    if low in ("all", "*", "any"):
        return None

    items = _parse_csv(raw)
    if not items:
        return None
    if len(items) == 1:
        return {"source": items[0]}
    return {"source": {"$in": items}}

def _env_required(keys: List[str], *, normalize_path: bool = False) -> str:
    v = _env_first(keys)
    if not v:
        joined = ", ".join(keys)
        raise RuntimeError(
            f"필수 환경변수 누락: {joined}\n"
            f"→ .env에 다음 중 하나를 설정하세요.\n"
            f"   {keys[0]}=YOUR_VALUE"
        )
    if normalize_path:
        v = str(Path(os.path.expandvars(os.path.expanduser(v))).resolve())
    return v


def _canon_path(p: Optional[str], *, default_subdir: str = "chroma") -> str:
    """
    경로 정규화 유틸.
    - 값이 없으면 BASE_DIR/default_subdir 사용
    - 제어문자(ASCII < 32)가 포함되면 기본 경로로 되돌림
      → C:\\x0bscode\\project 같은 잘못된 경로 방지
    """
    candidate = str(p) if p else str(BASE_DIR / default_subdir)
    candidate = os.path.expandvars(os.path.expanduser(candidate))
    if any(ord(ch) < 32 for ch in candidate):
        candidate = str(BASE_DIR / default_subdir)
    return str(Path(candidate).resolve())


def _is_localhost(host: str) -> bool:
    h = (host or "").strip().lower()
    return h in ("localhost", "127.0.0.1", "0.0.0.0") or h.endswith(".localhost")


def _manage_cmd() -> str:
    """
    manage.py <subcommand> 를 안전하게 추출.
    예: runserver / migrate / bootstrap_admin_pg ...
    """
    argv = sys.argv or []
    if len(argv) >= 3 and isinstance(argv[1], str) and argv[1].endswith("manage.py"):
        return argv[2].strip()
    if len(argv) >= 2 and isinstance(argv[1], str):
        return argv[1].strip()
    return ""


_MANAGE_CMD = _manage_cmd()
IS_DEPLOY_CHECK = (_MANAGE_CMD == "check" and "--deploy" in sys.argv)

IS_CLOUD_RUN_JOB = bool(os.environ.get("CLOUD_RUN_JOB") or os.environ.get("CLOUD_RUN_EXECUTION"))

# ✅ 이 커맨드들에서는 "웹/RAG용 필수 ENV" 강제 체크를 우회
_ENV_CHECK_BYPASS = IS_CLOUD_RUN_JOB or (_MANAGE_CMD in {
    "bootstrap_admin",
    "bootstrap_admin_pg",
    "adminreset_job",
    "adminreset_pg",
    "migrate",
    "makemigrations",
    "collectstatic",
    "purge_by_delete_at",
    "check",  # deploy-check/로컬체크 편의
})

IS_CLOUD_RUN = bool(
    os.environ.get("K_SERVICE")
    or os.environ.get("K_REVISION")
    or os.environ.get("CLOUD_RUN_JOB")
    or os.environ.get("CLOUD_RUN_JOB_NAME")
)

# ✅ Cloud Run 서비스(=웹) 여부
IS_CLOUD_RUN_SERVICE = bool(IS_CLOUD_RUN and (not IS_CLOUD_RUN_JOB))

# ─── Django 기본 ─────────────────────────────────────────────────────────────
SECRET_KEY = (
    _env_first(["DJANGO_SECRET_KEY", "SECRET_KEY"])
    or "dev-secret-key-only-for-local"
)

# ✅ Cloud Run 서비스에서는 SECRET_KEY 필수 (운영 사고 방지)
if IS_CLOUD_RUN_SERVICE and SECRET_KEY == "dev-secret-key-only-for-local":
    raise RuntimeError("Cloud Run 서비스에서는 DJANGO_SECRET_KEY(또는 SECRET_KEY)가 필수입니다.")

# ✅ 기본값 운영 친화적으로: DEBUG=0
DEBUG = _env_bool(["DJANGO_DEBUG"], default=False)

# 로컬에서 check --deploy 를 돌릴 때는 운영 모드 기준으로 검사되게 강제
if IS_DEPLOY_CHECK:
    DEBUG = False

STATIC_VERSION = os.environ.get("STATIC_VERSION") or os.environ.get("K_REVISION") or "dev"

# ✅ ALLOWED_HOSTS: Cloud Run 서비스에서 env 미설정이면 서비스 장애(DisallowedHost) 예방용으로 '*' 사용
_allowed_hosts_raw = (_env_first(["ALLOWED_HOSTS"], default="") or "").strip()
if _allowed_hosts_raw:
    ALLOWED_HOSTS = _parse_csv(_allowed_hosts_raw)
else:
    if IS_CLOUD_RUN_SERVICE:
        ALLOWED_HOSTS = ["*"]
        try:
            print("[WARN] ALLOWED_HOSTS is not set. Using '*' to avoid DisallowedHost. Set ALLOWED_HOSTS explicitly for better security.", file=sys.stderr)
        except Exception:
            pass
    else:
        ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

INSTALLED_APPS = [
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "channels",
    "ragapp.apps.RagappConfig",
    "ragapp.board",
    "ragapp.livechat",
    "storages",
]

ASGI_APPLICATION = "ragsite.asgi.application"
WSGI_APPLICATION = "ragsite.wsgi.application"

# ─── Channels ────────────────────────────────────────────────────────────────
CHANNEL_LAYERS: Dict[str, Any] = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    }
}
REDIS_URL = os.environ.get("REDIS_URL", "").strip()
if REDIS_URL:
    CHANNEL_LAYERS["default"] = {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [REDIS_URL]},
    }
else:
    # ✅ 운영 안정성: Cloud Run에서 인스턴스가 2개 이상 뜨면 InMemory는 깨질 수 있음
    if IS_CLOUD_RUN_SERVICE:
        try:
            print("[WARN] REDIS_URL is not set. CHANNEL_LAYERS uses InMemory; websockets/history may be inconsistent if multiple instances run.", file=sys.stderr)
        except Exception:
            pass

# ─── Middleware ──────────────────────────────────────────────────────────────
ADMIN_OBS_BADGE_ALLOWED = True
ADMIN_OBS_BADGE_STAFF_ONLY = True
ENV_NAME = "prod"

# ✅ 업로드 쿼터는 UsageQuotaMiddleware에서만 차감(중복 방지)
REQUEST_GUARD_ENABLE_DAILY_UPLOAD_QUOTA = False

# ✅ (NEW) 전역 욕설/모욕/성희롱 입력 차단
CONTENT_GUARD_ENABLED = _env_bool(["CONTENT_GUARD_ENABLED"], default=True)

# ✅ 400 대신 200으로 (프론트 fetch가 "에러 경로"로 빠지지 않게)
CONTENT_GUARD_BLOCK_STATUS = _env_int(["CONTENT_GUARD_BLOCK_STATUS"], default=200)

CONTENT_GUARD_ALLOW_PREFIXES = (
    "/static/",
    "/legal/",
    "/favicon.ico",
    "/healthz",
    # 관리자 화면에는 날짜, 내부 ID, 검색 필터처럼 숫자로 된 제어값이
    # 자동으로 포함된다. 이를 사용자 입력 PII로 검사하면 새로고침만 해도
    # 오탐 차단 페이지가 표시되므로 전역 가드에서는 제외한다.
    # 공개 입력 API와 상담/검색 기능의 개별 PII 검사는 계속 적용된다.
    "/admin/",
    "/ragadmin/",
)

# ✅ (NEW) 강력 경고 문구(원하면 여기만 바꾸면 됨)
CONTENT_GUARD_MESSAGE_ABUSE = (
    "🚫 욕설/모욕 표현은 허용되지 않습니다.\n"
    "정중한 표현으로 다시 작성해 주세요."
)
CONTENT_GUARD_MESSAGE_SEXUAL = (
    "🚫 성희롱/음란/성적 모욕 표현은 허용되지 않습니다.\n"
    "정중한 표현으로 다시 작성해 주세요."
)

# ─────────────────────────────────────────────
# ✅ (NEW) 전역 개인정보(PII) 입력 차단 (카드/계좌/주민/전화/이메일/주소 등)
# - 기존 코드는 건들지 않고, 설정만 "추가"로 붙입니다.
# ─────────────────────────────────────────────
PII_BLOCK_ENABLED = _env_bool(["PII_BLOCK_ENABLED"], default=True)

# ✅ 400 대신 200으로 (프론트 fetch가 "에러 경로"로 빠지지 않게)
PII_BLOCK_STATUS = _env_int(["PII_BLOCK_STATUS"], default=200)

# 너무 큰 요청 바디는 전체 스캔하지 않음(기본 200KB)
PII_BLOCK_MAX_SCAN_BYTES = _env_int(["PII_BLOCK_MAX_SCAN_BYTES"], default=200_000)

# ContentGuard와 동일하게 기본 allow/정적/헬스 등 제외
# + 스태프 전용 검수(moderation) API: pending_id/actor_key 같은 내부 hex ID가
#   우연히 5자리 숫자(우편번호)나 10~20자리 숫자(계좌번호) 패턴과 겹쳐 오탐되면서
#   요청이 뷰까지 도달하지 못하고 200으로 조용히 막히는 문제가 있었음(승인/거절 API 전반)
PII_BLOCK_EXCLUDED_PATH_PREFIXES = CONTENT_GUARD_ALLOW_PREFIXES + (
    "/api/media/pending/",
    "/api/media/rejected/",
    "/api/media/approved/",
    "/api/moderation/",
)

# 감지 항목(kind)을 응답에 포함할지(원하면 False)
PII_BLOCK_INCLUDE_KIND = _env_bool(["PII_BLOCK_INCLUDE_KIND"], default=True)

# 원하면 차단 응답을 404로 위장(스텔스)
PII_BLOCK_STEALTH_404 = _env_bool(["PII_BLOCK_STEALTH_404"], default=False)

# ✅ 친절 안내 멘트(원하면 여기만 바꾸면 됨)
PII_BLOCK_TITLE = "민감정보 입력이 감지되었습니다"
PII_BLOCK_MESSAGE = (
    "카드번호/계좌번호/주민번호/전화번호/이메일/주소 등 개인정보는 입력할 수 없습니다.\n"
    "해당 내용을 삭제(또는 마스킹)한 뒤 다시 시도해주세요.\n"
    "예) 1234-****-****-5678 / ****1234"
)

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",

    # ✅ (NEW) 스캐너/프로브 요청 즉시 차단 (/.env, /.git 등) — 가장 위에서 컷!
    "ragapp.middleware.security.BlockProbesMiddleware",

    "whitenoise.middleware.WhiteNoiseMiddleware",

    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",

    # ✅ (NEW) 전역 개인정보(PII) 차단 — 뷰/DB/LLM 전에 컷
    "ragapp.middleware.pii_block.PiiBlockMiddleware",

    # ✅ (NEW) 전역 욕설/모욕/성희롱 차단 — 뷰/DB/LLM 전에 컷
    "ragapp.middleware.content_guard_middleware.ContentGuardMiddleware",

    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",

    # ✅ (NEW) 이미지 기능 연타 방지(서버 안전망)
    "ragapp.middleware.image_throttle_middleware.ImageThrottleMiddleware",

    # ✅ DB 보호(동시 DB 진입 제한 + 쿨링)
    "ragapp.middleware.db_guard_middleware.DBGuardMiddleware",

    # ✅ (NEW) /admin/, /ragadmin/ 접근 시 admin_count +1
    "ragapp.middleware.admin_usage_middleware.AdminUsageMiddleware",

    # ✅ 전 기능 차단(정지/영구정지) — 세션/유저 필요해서 여기!
    "ragapp.middleware.penalty_middleware.PenaltyMiddleware",

    # ✅ 요청 ID는 OBS가 사용하므로, OBS보다 먼저
    "ragapp.middleware.request_id.RequestIdMiddleware",

    # ✅ 운영 컨트롤(점검/쓰기잠금/스파이크 + 접속자)
    "ragapp.middleware.ops_control.OpsControlMiddleware",

    # ✅ 요청 가드
    "ragapp.middleware.request_guard.RequestGuardMiddleware",

    # ✅ 업로드 쿼터(단일화)
    "ragapp.middleware.usage_quota_middleware.UsageQuotaMiddleware",

    # ✅ 게시판 보호
    "ragapp.middleware.board_content_guard.BoardContentGuardMiddleware",

    # (선택) 별도 트래커가 이미 있으면 유지해도 됨
    "ragapp.middleware.online_tracker.OnlineTrackerMiddleware",

    # ✅ admin 뱃지/세션기반 OBS
    "ragapp.middleware.admin_obs_badge.AdminObservabilityBadgeMiddleware",

    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",

    "ragapp.middleware.legal_noindex.LegalSecurityHeadersMiddleware",
    "ragapp.middleware.legal_noindex.NoIndexMiddleware",
]


ROOT_URLCONF = "ragsite.urls"

# ─── Templates ───────────────────────────────────────────────────────────────
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [
            BASE_DIR / "templates",
            BASE_DIR / "ragapp" / "templates",
        ],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "ragapp.context_processors.legal_context",
                "ragapp.news_views.context_processors.usage_limits",
                "ragapp.context_processors.static_version",
            ],
        },
    },
]

# ─── Cache ───────────────────────────────────────────────────────────────────
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "ragapp-guard",
    }
}

# ✅ 중복 차감 방지:
REQUEST_GUARD_DAILY_QUOTA_UPLOAD_MAP = {}
REQUEST_GUARD_ENABLE_DAILY_UPLOAD_QUOTA = False

USAGE_QUOTA_UPLOAD_MAP = {
    "/media/index/": "image",
    "/table/index/": "table",
}

# ─── 벡터 SQLite DB 경로 ─────────────────────────────────────────────────────
_vector_db_raw = _env_first(
    ["VECTOR_DB_PATH"],
    default=str(BASE_DIR / "sqlite3" / "vector_store.sqlite3"),
) or str(BASE_DIR / "sqlite3" / "vector_store.sqlite3")
VECTOR_DB_PATH = str(Path(os.path.expandvars(os.path.expanduser(_vector_db_raw))).resolve())
os.environ.setdefault("VECTOR_DB_PATH", VECTOR_DB_PATH)

# ─── DB ──────────────────────────────────────────────────────────────────────
_db_host = (_env_first(["DB_HOST", "DATABASE_HOST", "PGHOST"], default="") or "").strip()
_db_name = (_env_first(["DB_NAME", "DATABASE_NAME", "PGDATABASE"], default="") or "").strip()
_db_user = (_env_first(["DB_USER", "DATABASE_USER", "PGUSER"], default="") or "").strip()
_db_pass = (_env_first(["DB_PASSWORD", "DATABASE_PASSWORD", "PGPASSWORD"], default="") or "").strip()
_db_port = (_env_first(["DB_PORT", "PGPORT"], default="") or "").strip()
_sqlite_db_path = (_env_first(["SQLITE_DB_PATH"], default="") or "").strip()

DB_CONN_MAX_AGE = _env_int(["DB_CONN_MAX_AGE", "CONN_MAX_AGE"], default=30)             # 초 (0도 가능)
DB_CONNECT_TIMEOUT = _env_int(["DB_CONNECT_TIMEOUT"], default=5)         # 초
DB_STATEMENT_TIMEOUT_MS = _env_int(["DB_STATEMENT_TIMEOUT_MS"], default=8000)  # ms
DB_LOCK_TIMEOUT_MS = _env_int(["DB_LOCK_TIMEOUT_MS"], default=2000)            # ms

if IS_CLOUD_RUN_SERVICE:
    if not (_db_host and _db_name and _db_user and _db_pass):
        raise RuntimeError(
            "Cloud Run 서비스에서 Postgres 환경변수가 누락되어 sqlite로 폴백될 수 있어요. "
            "DB_HOST/DB_NAME/DB_USER/DB_PASSWORD를 모두 설정하세요."
        )

if _db_host and _db_name and _db_user:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": _db_name,
            "USER": _db_user,
            "PASSWORD": _db_pass,
            "HOST": _db_host,
            "PORT": _db_port,

            # ✅ 연결 재사용(기본 30초). "최대 연결수 더 낮추고 싶으면" 0으로도 가능(대신 매 요청 연결)
            "CONN_MAX_AGE": DB_CONN_MAX_AGE,

            "CONN_HEALTH_CHECKS": True,

            # ✅ DB 보호용 타임아웃(폭주/락 오래 잡는 상황 컷)
            "OPTIONS": {
                "connect_timeout": DB_CONNECT_TIMEOUT,
                "options": f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS} -c lock_timeout={DB_LOCK_TIMEOUT_MS}",
            },
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": Path(_sqlite_db_path) if _sqlite_db_path else BASE_DIR / "db.sqlite3",
        }
    }

# ─── 국제화/시간대 ────────────────────────────────────────────────────────────
LANGUAGE_CODE = "ko-kr"
TIME_ZONE = "Asia/Seoul"
USE_I18N = True
USE_TZ = True

# ─── 하루 사용량 ────────────────────────────────────────────────────────────
QA_USAGE_LIMIT_WEB = _env_int(["QA_USAGE_LIMIT_WEB"], default=7)
QA_USAGE_LIMIT_RAG = _env_int(["QA_USAGE_LIMIT_RAG"], default=7)
QA_USAGE_LIMIT_PDF = _env_int(["QA_USAGE_LIMIT_PDF"], default=5)
QA_USAGE_LIMIT_IMAGE = _env_int(["QA_USAGE_LIMIT_IMAGE"], default=10)
QA_USAGE_LIMIT_TABLE = _env_int(["QA_USAGE_LIMIT_TABLE"], default=0)

USAGE_LIMIT_WEB_DAILY = QA_USAGE_LIMIT_WEB
USAGE_LIMIT_RAG_DAILY = QA_USAGE_LIMIT_RAG
USAGE_LIMIT_PDF_DAILY = QA_USAGE_LIMIT_PDF
USAGE_LIMIT_IMAGE_DAILY = QA_USAGE_LIMIT_IMAGE
USAGE_LIMIT_TABLE_DAILY = QA_USAGE_LIMIT_TABLE

# ─── PDF 분석 제한 ───────────────────────────────────────────────────────────
PDF_ANALYZE_MAX_FILES = _env_int(["PDF_ANALYZE_MAX_FILES"], default=3)
PDF_ANALYZE_MAX_SIZE_MB = _env_int(["PDF_ANALYZE_MAX_SIZE_MB"], default=10)
PDF_ANALYZE_MAX_TOTAL_CHARS = _env_int(["PDF_ANALYZE_MAX_TOTAL_CHARS"], default=60000)

# ─── 런타임 대시보드(접속자 상태) 임계값 ─────────────────────────────────────
RUNTIME_ONLINE_WARN = _env_int(["RUNTIME_ONLINE_WARN"], default=2)
RUNTIME_ONLINE_BUSY = _env_int(["RUNTIME_ONLINE_BUSY"], default=5)
RUNTIME_ONLINE_TTL_SEC = _env_int(["RUNTIME_ONLINE_TTL_SEC"], default=60)
RUNTIME_ONLINE_HARD_LIMIT = _env_int(["RUNTIME_ONLINE_HARD_LIMIT"], default=10)

# ─── 정적파일 ────────────────────────────────────────────────────────────────
STATIC_URL = "/static/"
STATICFILES_DIRS = [
    p for p in [
        (BASE_DIR / "static") if (BASE_DIR / "static").exists() else None,
        (BASE_DIR / "ragapp" / "static") if (BASE_DIR / "ragapp" / "static").exists() else None,
    ] if p
]
STATIC_ROOT = BASE_DIR / "staticfiles"

# ─────────────────────────────────────────────────────────────────────────────
# ✅ 업로드 스토리지: Cloud Run "서비스"에서는 GCS 업로드 강제
#   - 로컬: USE_GCS_UPLOADS=0 가능
#   - Cloud Run 서비스: USE_GCS_UPLOADS가 0이어도 무시하고 True로 강제
#   - 버킷 이름이 없으면 서비스 부팅 실패(실수 방지)
# ─────────────────────────────────────────────────────────────────────────────
GS_BUCKET_NAME = (os.environ.get("GS_BUCKET_NAME") or "").strip()

# 흔한 실수 방지: "{GS_BUCKET_NAME}" 같은 플레이스홀더 그대로면 즉시 실패
if GS_BUCKET_NAME.startswith("{") and GS_BUCKET_NAME.endswith("}"):
    raise RuntimeError(f"GS_BUCKET_NAME looks like a placeholder: {GS_BUCKET_NAME}")

_env_flag_use_gcs = _env_bool(["USE_GCS_UPLOADS"], default=False)
if IS_CLOUD_RUN_SERVICE:
    USE_GCS_UPLOADS = True
    if (os.environ.get("USE_GCS_UPLOADS") or "").strip() in ("0", "false", "False", "no", "off"):
        try:
            print("[WARN] USE_GCS_UPLOADS=0 이지만 Cloud Run 서비스에서는 GCS 업로드를 강제로 켭니다.", file=sys.stderr)
        except Exception:
            pass
else:
    USE_GCS_UPLOADS = _env_flag_use_gcs

# GCS를 쓰겠다고 했는데 버킷이 없으면 즉시 실패
if USE_GCS_UPLOADS and not GS_BUCKET_NAME:
    raise RuntimeError(
        "GCS 업로드가 활성화되어 있는데 GS_BUCKET_NAME 이 비어있습니다. "
        "환경변수 GS_BUCKET_NAME 을 설정하세요."
    )

# 업로드 파일을 버킷 내 지정 prefix 아래로 정리
GS_LOCATION = (_env_first(["GS_LOCATION"], default="uploads") or "uploads").strip().strip("/")

# 파일명 충돌 방지(안전)
GS_FILE_OVERWRITE = False

# Uniform bucket-level access 권장: ACL 미사용
GS_DEFAULT_ACL = None

# 업로드 객체 메타(캐시)
GS_OBJECT_PARAMETERS = {
    "cache_control": "public, max-age=31536000, immutable",
}

# ✅ Storage backend 선택
if USE_GCS_UPLOADS:
    STORAGES = {
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
        "default": {
            "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
        },
    }
else:
    STORAGES = {
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
    }

# ─────────────────────────────────────────────
# ✅ django-storages 적용 보강 (Django<4.2 호환)
# ─────────────────────────────────────────────
if USE_GCS_UPLOADS:
    DEFAULT_FILE_STORAGE = "storages.backends.gcloud.GoogleCloudStorage"
    GS_QUERYSTRING_AUTH = False  # (선택) 공개 URL이면 서명 쿼리스트링 안 붙이기
else:
    DEFAULT_FILE_STORAGE = "django.core.files.storage.FileSystemStorage"

# ─────────────────────────────────────────────
# ✅ 미디어 URL/ROOT
#   - URL은 서비스 정책상 고정(/uploads/)
#   - Cloud Run 서비스에서는 FS 쓰더라도 /tmp/uploads로 강제(안전)
# ─────────────────────────────────────────────
MEDIA_URL = "/uploads/"

_media_default = "/tmp/uploads" if IS_CLOUD_RUN else str(BASE_DIR / "uploads")
MEDIA_ROOT = Path(_env_first(["MEDIA_ROOT"], default=_media_default) or _media_default)

# Cloud Run "서비스"에서는 MEDIA_ROOT를 /tmp로 강제 (env로 /workspace 덮임 방지)
if IS_CLOUD_RUN_SERVICE:
    MEDIA_ROOT = Path("/tmp/uploads")

# FileSystemStorage일 때만 폴더 생성
if (not USE_GCS_UPLOADS) and (not _ENV_CHECK_BYPASS):
    try:
        MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

# WhiteNoise 튜닝
WHITENOISE_USE_FINDERS = True if DEBUG else False
WHITENOISE_AUTOREFRESH = True if DEBUG else False
WHITENOISE_MAX_AGE = 0 if DEBUG else 60 * 60 * 24 * 365
STATICFILES_FINDERS = [
    "django.contrib.staticfiles.finders.FileSystemFinder",
    "django.contrib.staticfiles.finders.AppDirectoriesFinder",
]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ─── Chroma / RAG 기본 ──────────────────────────────────────────────────────
_chroma_default = "/tmp/chroma" if IS_CLOUD_RUN else str(BASE_DIR / "chroma")
CHROMA_DB_DIR = _canon_path(_env_first(["CHROMA_DB_DIR"], default=_chroma_default), default_subdir="chroma")

if not _ENV_CHECK_BYPASS:
    try:
        Path(CHROMA_DB_DIR).mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

# ─── Vertex 429 재시도(백오프) ───────────────────────────────────────────────
VERTEX_RETRY_MAX_ATTEMPTS = _env_int(["VERTEX_RETRY_MAX_ATTEMPTS"], default=4)
VERTEX_RETRY_BASE_SLEEP = _env_float(["VERTEX_RETRY_BASE_SLEEP"], default=1.0)
VERTEX_RETRY_MAX_SLEEP = _env_float(["VERTEX_RETRY_MAX_SLEEP"], default=8.0)

VERTEX_API_KEY = (
    os.environ.get("VERTEX_API_KEY")
    or os.environ.get("GOOGLE_API_KEY")
    or os.environ.get("GEMINI_API_KEY")
)
if isinstance(VERTEX_API_KEY, str) and VERTEX_API_KEY.strip():
    os.environ.setdefault("API_KEY", VERTEX_API_KEY.strip())
    os.environ.setdefault("GOOGLE_API_KEY", VERTEX_API_KEY.strip())
    os.environ.setdefault("GEMINI_API_KEY", VERTEX_API_KEY.strip())

if _ENV_CHECK_BYPASS:
    VERTEX_PROJECT_ID = _env_first(
        ["VERTEX_PROJECT_ID", "vertex_id", "GCP_PROJECT", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"],
        default="bootstrap",
    ) or "bootstrap"
else:
    VERTEX_PROJECT_ID = _env_required(
        ["VERTEX_PROJECT_ID", "vertex_id", "GCP_PROJECT", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"]
    )

# ─────────────────────────────────────────────────────────────────────────────
# Vertex / Gemini 설정
# ─────────────────────────────────────────────────────────────────────────────

# ✅ Gemini 생성 모델 전용 위치
# - gemini-3.5-flash는 global로 호출
# - GOOGLE_CLOUD_LOCATION / GCP_LOCATION은 기본 GCP 리전이라 여기서 읽지 않음
VERTEX_LLM_LOCATION = _env_first(
    ["VERTEX_LLM_LOCATION"],
    default="global",
) or "global"

# ✅ 임베딩 모델은 생성 모델과 리전을 분리
VERTEX_EMBED_LOCATION = _env_first(
    ["VERTEX_EMBED_LOCATION", "EMBED_LOCATION"],
    default="asia-northeast1",
) or "asia-northeast1"

# ✅ 기본 GCP/Vertex 리전: 도쿄 유지
# - Cloud Run / GCP 기본 위치용
# - Gemini 3.5 Flash 생성 호출은 VERTEX_LLM_LOCATION을 따로 사용
VERTEX_LOCATION = _env_first(
    ["VERTEX_LOCATION", "GCP_LOCATION", "GOOGLE_CLOUD_LOCATION"],
    default="asia-northeast1",
) or "asia-northeast1"

_gac = _env_first(["GOOGLE_APPLICATION_CREDENTIALS"], default="") or ""
GOOGLE_APPLICATION_CREDENTIALS = (
    str(Path(os.path.expandvars(os.path.expanduser(_gac))).resolve()) if _gac else ""
)

if GOOGLE_APPLICATION_CREDENTIALS and not IS_CLOUD_RUN:
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = GOOGLE_APPLICATION_CREDENTIALS
else:
    if IS_CLOUD_RUN and "GOOGLE_APPLICATION_CREDENTIALS" in os.environ:
        os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)

os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
os.environ.setdefault("VERTEX_PROJECT_ID", VERTEX_PROJECT_ID)
os.environ.setdefault("GCP_PROJECT", VERTEX_PROJECT_ID)
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", VERTEX_PROJECT_ID)
os.environ.setdefault("GCLOUD_PROJECT", VERTEX_PROJECT_ID)

# ✅ Gemini 생성 모델용 위치
os.environ.setdefault("VERTEX_LLM_LOCATION", VERTEX_LLM_LOCATION)
os.environ.setdefault("GEMINI_LOCATION", VERTEX_LLM_LOCATION)

# ✅ 기본 GCP/Vertex 위치
os.environ.setdefault("VERTEX_LOCATION", VERTEX_LOCATION)
os.environ.setdefault("GCP_LOCATION", VERTEX_LOCATION)

# 주의:
# GOOGLE_CLOUD_LOCATION은 일부 Gemini SDK가 기본 location으로 읽을 수 있음.
# gemini-3.5-flash는 global 호출이 안전하므로 LLM 위치로 맞춤.
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", VERTEX_LLM_LOCATION)

# ✅ 임베딩용 위치
os.environ.setdefault("VERTEX_EMBED_LOCATION", VERTEX_EMBED_LOCATION)

GENAI_API_VERSION = _env_first(["GENAI_API_VERSION"], default="v1") or "v1"

# ✅ Gemini 생성 모델
GEMINI_MODEL = _env_first(
    ["GEMINI_MODEL", "GEMINI_MODEL_DIRECT", "GEMINI_TEXT_MODEL", "VERTEX_TEXT_MODEL"],
    default="gemini-3.5-flash",
) or "gemini-3.5-flash"

GEMINI_TEXT_MODEL = _env_first(["GEMINI_TEXT_MODEL"], default=GEMINI_MODEL) or GEMINI_MODEL
VERTEX_TEXT_MODEL = _env_first(["VERTEX_TEXT_MODEL"], default=GEMINI_MODEL) or GEMINI_MODEL
GEMINI_MODEL_DIRECT = _env_first(["GEMINI_MODEL_DIRECT"], default=GEMINI_MODEL) or GEMINI_MODEL
GEMINI_MODEL_RAG = _env_first(["GEMINI_MODEL_RAG"], default=GEMINI_MODEL) or GEMINI_MODEL

# ✅ PDF Gemini 전용 설정
# - PDF 검색/분석 쪽에서 모델과 location을 따로 읽게 하기 위함
# - gemini-3.5-flash는 global 호출이 안전
GEMINI_MODEL_PDF = _env_first(
    ["GEMINI_MODEL_PDF", "PDF_GEMINI_MODEL", "PDF_VERTEX_MODEL"],
    default=GEMINI_MODEL,
) or GEMINI_MODEL

PDF_VERTEX_LOCATION = _env_first(
    ["PDF_VERTEX_LOCATION", "PDF_GEMINI_LOCATION"],
    default=VERTEX_LLM_LOCATION,
) or VERTEX_LLM_LOCATION

os.environ.setdefault("GEMINI_MODEL_PDF", GEMINI_MODEL_PDF)
os.environ.setdefault("PDF_VERTEX_LOCATION", PDF_VERTEX_LOCATION)

# ✅ 텍스트 임베딩 모델
_embed_src = _env_first(
    ["EMBED_MODEL", "GEMINI_EMBED_MODEL", "GEMINI_EMBED_MODELS"],
    default="text-embedding-005",
) or "text-embedding-005"

GEMINI_EMBED_MODELS = [m.strip() for m in _embed_src.split(",") if m.strip()]

VERTEX_EMBED_MODEL = _env_first(
    ["VERTEX_EMBED_MODEL", "EMBED_MODEL", "GEMINI_EMBED_MODEL"],
    default=(GEMINI_EMBED_MODELS[0] if GEMINI_EMBED_MODELS else "text-embedding-005"),
) or (GEMINI_EMBED_MODELS[0] if GEMINI_EMBED_MODELS else "text-embedding-005")

WEB_INGEST_TO_CHROMA = _env_bool(["WEB_INGEST_TO_CHROMA"], default=True)
AUTO_INGEST_AFTER_GEMINI = _env_bool(["AUTO_INGEST_AFTER_GEMINI"], default=True)

# ✅ 안전 정책상 답변 링크 본문 크롤링은 기본 OFF
CRAWL_ANSWER_LINKS = _env_bool(["CRAWL_ANSWER_LINKS"], default=False)

ANSWER_LINK_MAX = _env_int(["ANSWER_LINK_MAX"], default=5)
ANSWER_LINK_TIMEOUT = _env_int(["ANSWER_LINK_TIMEOUT"], default=12)
MIN_NEWS_BODY_CHARS = _env_int(["MIN_NEWS_BODY_CHARS"], default=400)
EMBED_CHUNK_SIZE = _env_int(["EMBED_CHUNK_SIZE"], default=1600)
EMBED_CHUNK_OVERLAP = _env_int(["EMBED_CHUNK_OVERLAP"], default=200)
NEWS_TOPK = _env_int(["NEWS_TOPK"], default=5)

# ✅ 뉴스 크롤링/인덱싱 안전 정책
WEB_INGEST_META_ONLY = _env_bool(["WEB_INGEST_META_ONLY"], default=True)
ALLOW_STORE_NEWS_BODY = _env_bool(["ALLOW_STORE_NEWS_BODY"], default=False)
NEWS_SNIPPET_INDEX_CHARS = _env_int(["NEWS_SNIPPET_INDEX_CHARS"], default=300)

RAG_FORCE_ANSWER = _env_bool(["RAG_FORCE_ANSWER"], default=True)
RAG_QUERY_TOPK = _env_int(["RAG_QUERY_TOPK"], default=5)
RAG_FALLBACK_TOPK = _env_int(["RAG_FALLBACK_TOPK"], default=12)
RAG_MAX_SOURCES = _env_int(["RAG_MAX_SOURCES"], default=8)

# ✅ RAG 검색 대상 정규화
# 테스트: RAG_SOURCES_FILTER=news
# 전체검색: RAG_SOURCES_FILTER=all
# 기본값에서는 AI 생성 답변(web_answer)을 제외해서 뉴스/업로드 근거가 우선 잡히게 함
RAG_SOURCES_FILTER = _rag_sources_where_from_env(
    default_csv="upload_doc,news,answer_link"
)

RAG_EVIDENCE_MAX_CARDS = 5
RAG_EVIDENCE_MIN_SCORE = None

NEWS_RSS_QUERY_TEMPLATE = _env_first(
    ["NEWS_RSS_QUERY_TEMPLATE"],
    default="https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko",
) or "https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"

USE_HEADLESS_BROWSER = True
HEADLESS_TIMEOUT_SEC = _env_int(["HEADLESS_TIMEOUT_SEC"], default=12)

SAFE_MODE_ENABLED = _env_bool(["SAFE_MODE_ENABLED"], default=True)
SAFE_ROBOTS_RESPECT = _env_bool(["SAFE_ROBOTS_RESPECT"], default=True)
SAFE_SUMMARY_ONLY = _env_bool(["SAFE_SUMMARY_ONLY"], default=True)
SAFE_USE_LLM_SUMMARY = _env_bool(["SAFE_USE_LLM_SUMMARY"], default=True)
SAFE_MIN_BODY_CHARS = _env_int(["SAFE_MIN_BODY_CHARS"], default=600)
SAFE_STRICT_DELETE = _env_bool(["SAFE_STRICT_DELETE"], default=True)

STORE_FULLTEXT = _env_bool(["STORE_FULLTEXT"], default=False)

RESPECT_ROBOTS = _env_bool(["RESPECT_ROBOTS", "SAFE_ROBOTS_RESPECT"], default=True)
ROBOTS_RESPECT_RAW = (_env_first(["ROBOTS_RESPECT"], default="") or "").strip().lower()
ROBOTS_RESPECT = RESPECT_ROBOTS if ROBOTS_RESPECT_RAW == "" else (ROBOTS_RESPECT_RAW not in ("0", "false", "no"))

CRAWL_RATE_LIMIT_PER_HOST = _env_float(["CRAWL_RATE_LIMIT_PER_HOST"], default=1.0)
CRAWL_PER_HOST_RPS = _env_float(["CRAWL_PER_HOST_RPS"], default=CRAWL_RATE_LIMIT_PER_HOST)
CRAWL_USER_AGENT = _env_first(
    ["CRAWL_USER_AGENT"],
    default="ragapp-bot/1.0 (+contact@example.com)",
) or "ragapp-bot/1.0 (+contact@example.com)"

ALLOWLIST_DOMAINS = _env_csv(["ALLOWLIST_DOMAINS"], default="news.google.com")
ALLOWED_NEWS_DOMAINS = _env_csv(["ALLOWED_NEWS_DOMAINS"], default="")

LOG_IP_HASHED = _env_bool(["LOG_IP_HASHED"], default=True)
LOG_IP_HASH_SECRET = _env_first(["LOG_IP_HASH_SECRET"], default=SECRET_KEY) or SECRET_KEY

RETENTION_DAYS = _env_int(["RETENTION_DAYS", "RETENTION_DAYS_DEFAULT"], default=30)
RETENTION_DAYS_DEFAULT = RETENTION_DAYS

LOG_RETENTION_DAYS = _env_int(["LOG_RETENTION_DAYS"], default=30)
ANONYMIZE_IP = _env_bool(["ANONYMIZE_IP"], default=True)

RETENTION_DAYS_CHATLOG = _env_int(["RETENTION_DAYS_CHATLOG"], default=30)
RETENTION_DAYS_FEEDBACK = _env_int(["RETENTION_DAYS_FEEDBACK"], default=30)
RETENTION_DAYS_QARAG_FEEDBACK = _env_int(["RETENTION_DAYS_QARAG_FEEDBACK"], default=30)
RETENTION_DAYS_CONSENT = _env_int(["RETENTION_DAYS_CONSENT"], default=365)

RETENTION_DAYS_APPLOG = _env_int(["RETENTION_DAYS_APPLOG"], default=LOG_RETENTION_DAYS)
RETENTION_DAYS_MYLOG = _env_int(["RETENTION_DAYS_MYLOG"], default=90)
RETENTION_DAYS_INGESTHISTORY = _env_int(["RETENTION_DAYS_INGESTHISTORY"], default=180)
RETENTION_DAYS_LIVECHAT = _env_int(["RETENTION_DAYS_LIVECHAT"], default=180)

_default_allowlist = [
    "ragapp.ChatQueryLog",
    "ragapp.Feedback",
    "ragapp.FeedbackLog",
    "ragapp.QaragFeedback",
    "ragapp.MyLog",
    "ragapp.IngestHistory",
    "ragapp.AppLog",
    "ragapp.LiveChatSession",
    "ragapp.LiveChatRoom",
]
AUTO_PURGE_MODEL_ALLOWLIST = _env_csv(
    ["AUTO_PURGE_MODEL_ALLOWLIST"],
    default=",".join(_default_allowlist),
)

AUTO_PURGE_ENABLED = _env_bool(["AUTO_PURGE_ENABLED"], default=False)
AUTO_PURGE_BATCH_SIZE = _env_int(["AUTO_PURGE_BATCH_SIZE"], default=1000)

AUTO_PURGE_RETENTION_BY_MODEL: Dict[str, int] = {
    "ragapp.ChatQueryLog": RETENTION_DAYS_CHATLOG,
    "ragapp.Feedback": RETENTION_DAYS_FEEDBACK,
    "ragapp.FeedbackLog": RETENTION_DAYS_FEEDBACK,
    "ragapp.QaragFeedback": RETENTION_DAYS_QARAG_FEEDBACK,
    "ragapp.MyLog": RETENTION_DAYS_MYLOG,
    "ragapp.IngestHistory": RETENTION_DAYS_INGESTHISTORY,
    "ragapp.AppLog": RETENTION_DAYS_APPLOG,
    "ragapp.LiveChatSession": RETENTION_DAYS_LIVECHAT,
    "ragapp.LiveChatRoom": RETENTION_DAYS_LIVECHAT,
    "ragapp.ConsentLog": RETENTION_DAYS_CONSENT,
}

DATA_RETENTION_DAYS_DEFAULT = _env_int(
    ["DATA_RETENTION_DAYS_DEFAULT"],
    default=RETENTION_DAYS_DEFAULT,
)
DATA_RETENTION_DAYS_SECURITY_DEFAULT = _env_int(
    ["DATA_RETENTION_DAYS_SECURITY_DEFAULT"],
    default=365,
)
DATA_RETENTION_DAYS_BY_MODEL: Dict[str, int] = dict(AUTO_PURGE_RETENTION_BY_MODEL)
DATA_RETENTION_SECURITY_MODELS: List[str] = _env_csv(
    ["DATA_RETENTION_SECURITY_MODELS"],
    default="ragapp.AppLog,ragapp.MyLog,board.BoardAdminActionLog",
)
PURGE_DELETE_AT_MODELS: List[str] = _env_csv(
    ["PURGE_DELETE_AT_MODELS"],
    default=",".join(AUTO_PURGE_MODEL_ALLOWLIST),
)

CONTACT_EMAIL = _env_first(
    ["CONTACT_EMAIL", "ADMIN_CONTACT_EMAIL", "SUPPORT_EMAIL"],
    default="contact@example.com",
) or "contact@example.com"

LEGAL_DOCS_VERSION = _env_first(["LEGAL_DOCS_VERSION"], default="v1.0") or "v1.0"
LEGAL_EFFECTIVE_DATE = _env_first(["LEGAL_EFFECTIVE_DATE"], default="2025-11-12") or "2025-11-12"

PRIVACY_PAGE_URL = _env_first(["PRIVACY_PAGE_URL"], default="/legal/privacy/") or "/legal/privacy/"
TERMS_PAGE_URL = _env_first(["TERMS_PAGE_URL"], default="") or ""
COPYRIGHT_PAGE_URL = _env_first(["COPYRIGHT_PAGE_URL"], default="") or ""
SITEMAP_URL = _env_first(["SITEMAP_URL"], default="") or ""

NOINDEX_ENABLED = _env_bool(["NOINDEX_ENABLED"], default=True)

VERTEX_ZERO_DATA_RETENTION = _env_bool(["VERTEX_ZERO_DATA_RETENTION"], default=False)
VERTEX_DISABLE_GROUNDING = _env_bool(["VERTEX_DISABLE_GROUNDING"], default=False)

CONSENT_VERSION = _env_first(["CONSENT_VERSION"], default="v2025-11-02") or "v2025-11-02"

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = _env_bool(["USE_X_FORWARDED_HOST"], default=(not DEBUG))

PUBLIC_BASE_URL = (_env_first(["PUBLIC_BASE_URL", "BASE_URL", "SITE_URL"], default="") or "").strip().rstrip("/")

SECURE_SSL_REDIRECT = _env_bool(["SECURE_SSL_REDIRECT"], default=(not DEBUG))
SESSION_COOKIE_SECURE = _env_bool(["SESSION_COOKIE_SECURE"], default=(not DEBUG))
CSRF_COOKIE_SECURE = _env_bool(["CSRF_COOKIE_SECURE"], default=(not DEBUG))

SECURE_HSTS_SECONDS = _env_int(["SECURE_HSTS_SECONDS"], default=0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = _env_bool(["SECURE_HSTS_INCLUDE_SUBDOMAINS"], default=False)
SECURE_HSTS_PRELOAD = _env_bool(["SECURE_HSTS_PRELOAD"], default=False)
SECURE_REFERRER_POLICY = _env_first(
    ["SECURE_REFERRER_POLICY"],
    default="strict-origin-when-cross-origin",
) or "strict-origin-when-cross-origin"

SESSION_COOKIE_SAMESITE = _env_first(["SESSION_COOKIE_SAMESITE"], default="Lax") or "Lax"
CSRF_COOKIE_SAMESITE = _env_first(["CSRF_COOKIE_SAMESITE"], default="Lax") or "Lax"

SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"

# ✅ CSRF_TRUSTED_ORIGINS: 한 번만 계산/대입 (운영에서는 https만 허용)
_csrf_raw = _env_first(
    ["CSRF_TRUSTED_ORIGINS"],
    default="http://127.0.0.1:8000,http://localhost:8000",
) or ""
CSRF_TRUSTED_ORIGINS = _parse_csv(_csrf_raw)

if not DEBUG:
    CSRF_TRUSTED_ORIGINS = [o for o in CSRF_TRUSTED_ORIGINS if str(o).startswith("https://")]

    if not CSRF_TRUSTED_ORIGINS:
        if PUBLIC_BASE_URL.startswith("https://"):
            CSRF_TRUSTED_ORIGINS = [PUBLIC_BASE_URL]
        else:
            candidates = [h for h in ALLOWED_HOSTS if h and h != "*" and not _is_localhost(h)]
            if candidates:
                CSRF_TRUSTED_ORIGINS = [f"https://{candidates[0]}"]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ─── LOGGING (DB 핸들러는 운영/PG에서만) ─────────────────────────────────────
_is_pg = str(DATABASES.get("default", {}).get("ENGINE", "")).endswith("postgresql")
USE_DB_LOG_HANDLER = bool(
    _is_pg and (not _ENV_CHECK_BYPASS) and _env_bool(["USE_DB_LOG_HANDLER"], default=False)
)

_handlers: Dict[str, Dict[str, Any]] = {
    "console": {
        "class": "logging.StreamHandler",
        "formatter": "verbose",
    }
}
if USE_DB_LOG_HANDLER:
    _handlers["db"] = {
        "class": "ragapp.services.logging_handlers.DBLogHandler",
        "formatter": "verbose",
    }

_ragapp_handlers: List[str] = ["console"] + (["db"] if USE_DB_LOG_HANDLER else [])

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{levelname}] {asctime} {name} :: {message}",
            "style": "{",
        },
        "simple": {
            "format": "{levelname} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": _handlers,
    "loggers": {
        "ragapp": {
            "handlers": _ragapp_handlers,
            "level": "INFO",
            "propagate": False,
        },
        "ragapp.services.news_services": {
            "handlers": _ragapp_handlers,
            "level": "DEBUG",
            "propagate": False,
        },
        "ragapp.news_views.news_views": {
            "handlers": _ragapp_handlers,
            "level": "DEBUG",
            "propagate": False,
        },
        "django": {
            "handlers": ["console"],
            "level": "INFO",
        },
    },
}

LEGAL_NOINDEX = True

VDB_MIRROR_TO_RAGCHUNK = True
RAGCHUNK_MIRROR_ENABLED = True
RAGCHUNK_MIRROR_BATCH = 500

AUTO_PURGE_ALLOWLIST = [m.split(".", 1)[-1] for m in AUTO_PURGE_MODEL_ALLOWLIST]
AUTO_PURGE_NEVER = [
    "RagChunk", "LegalConfig", "RagSetting", "FaqEntry", "TableSchema", "TableSearchRule"
]
AUTO_PURGE_RETENTION_DAYS = {
    k.split(".", 1)[-1]: v for k, v in AUTO_PURGE_RETENTION_BY_MODEL.items()
}

SERVICE_NAME = _env_first(["SERVICE_NAME"], default="김동건의 포트폴리오") or "김동건의 포트폴리오"
OPERATOR_NAME = _env_first(["OPERATOR_NAME"], default="김동건") or "김동건"
GEMINI_MODEL_DEFAULT = GEMINI_MODEL

CHAT_RETENTION_DAYS = _env_int(["CHAT_RETENTION_DAYS"], default=30)
ABUSE_RETENTION_DAYS = _env_int(["ABUSE_RETENTION_DAYS"], default=365)
LEGAL_HOLD_RETENTION_DAYS = _env_int(["LEGAL_HOLD_RETENTION_DAYS"], default=1825)

LIVECHAT_PERSIST_MESSAGES = _env_bool(["LIVECHAT_PERSIST_MESSAGES"], default=True)
LIVECHAT_PERSIST_DEBUG = _env_bool(["LIVECHAT_PERSIST_DEBUG"], default=False)

# ─────────────────────────────────────────────
# ✅ LiveChat 보호 설정
# - WS 접속 수 카운터 제한은 기본 OFF
# - 서버 보호는 Cloud Run 설정 + 상담 세션 제한 + 메시지 rate limit으로 처리
# ─────────────────────────────────────────────
LIVECHAT_WS_LIMIT_ENABLED = _env_bool(["LIVECHAT_WS_LIMIT_ENABLED"], default=False)

# WS 카운터 제한을 다시 켤 경우를 위한 예비값
LIVECHAT_MAX_WS_CONNECTIONS = _env_int(["LIVECHAT_MAX_WS_CONNECTIONS"], default=100)
LIVECHAT_MAX_WS_CONNECTIONS_PER_ROOM = _env_int(["LIVECHAT_MAX_WS_CONNECTIONS_PER_ROOM"], default=20)
LIVECHAT_WS_SLOT_TTL = _env_int(["LIVECHAT_WS_SLOT_TTL"], default=300)

# 상담 요청 대기열 제한
LIVECHAT_MAX_WAITING_SESSIONS = _env_int(["LIVECHAT_MAX_WAITING_SESSIONS"], default=3)

# 동시에 진행 가능한 상담 수
LIVECHAT_MAX_ACTIVE_SESSIONS = _env_int(["LIVECHAT_MAX_ACTIVE_SESSIONS"], default=1)

# 오래된 waiting 상담 자동 종료 기준
LIVECHAT_WAITING_EXPIRE_MINUTES = _env_int(["LIVECHAT_WAITING_EXPIRE_MINUTES"], default=30)
LIVECHAT_ACTIVE_EXPIRE_MINUTES = _env_int(["LIVECHAT_ACTIVE_EXPIRE_MINUTES"], default=120)

# 고객 메시지 도배 방지
LIVECHAT_USER_MSG_LIMIT_PER_MIN = _env_int(["LIVECHAT_USER_MSG_LIMIT_PER_MIN"], default=20)

# ─────────────────────────────────────────────
# ✅ 정책 위반 시 즉시 종료 여부(consumer의 _end_on()에서 사용)
# ─────────────────────────────────────────────
LIVECHAT_END_ON_ABUSE = _env_bool(["LIVECHAT_END_ON_ABUSE"], default=True)
LIVECHAT_END_ON_SEXUAL = _env_bool(["LIVECHAT_END_ON_SEXUAL"], default=True)
LIVECHAT_END_ON_PII = _env_bool(["LIVECHAT_END_ON_PII"], default=True)

# ─────────────────────────────────────────────
# ✅ 욕설/성희롱 패턴(consumer의 lazy compile에서 사용)
#   (원하면 여기서 직접 튜닝)
# ─────────────────────────────────────────────
LIVECHAT_ABUSE_PATTERNS = [
    r"(씨\s*발|ㅅ\s*ㅂ)",
    r"(병\s*신)",
    r"(개\s*새\s*끼)",
    r"(좆)",
    r"(멍청(하|해))",
]
LIVECHAT_SEXUAL_PATTERNS = [
    r"(성희롱)",
    r"(섹스)",
    r"(야동)",
]

LIVECHAT_ABUSIVE_KEYWORDS = ["ㅅㅂ", "씨발", "병신", "좆", "ㅈㄹ"]
EVIDENCE_ADMIN_GROUP = _env_first(["EVIDENCE_ADMIN_GROUP"], default="evidence_admin") or "evidence_admin"
LIVECHAT_ABUSE_RETENTION_DAYS = _env_int(["LIVECHAT_ABUSE_RETENTION_DAYS"], default=365)
LIVECHAT_RETENTION_DAYS = _env_int(["LIVECHAT_RETENTION_DAYS"], default=30)

if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

    SESSION_COOKIE_HTTPONLY = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"
    SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"

    if int(SECURE_HSTS_SECONDS or 0) <= 0:
        SECURE_HSTS_SECONDS = 3600
    SECURE_HSTS_INCLUDE_SUBDOMAINS = bool(SECURE_HSTS_INCLUDE_SUBDOMAINS)

    SECURE_HSTS_PRELOAD = bool(SECURE_HSTS_PRELOAD) if _env_first(["SECURE_HSTS_PRELOAD"], default="") else False

    WHITENOISE_USE_FINDERS = False
    WHITENOISE_AUTOREFRESH = False
