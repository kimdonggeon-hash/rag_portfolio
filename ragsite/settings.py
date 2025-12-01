# ragsite/settings.py
from pathlib import Path
import os

# ─── BASE ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent

# ─── .env 로드 ───────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except Exception:
    pass

# ─── 유틸: 환경변수 로딩 ────────────────────────────────────────────────────
def _dequote(v: str | None) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1].strip()
    return s

def _env_first(keys: list[str], *, default: str | None = None) -> str | None:
    for k in keys:
        v = os.environ.get(k)
        v = _dequote(v)
        if v is not None and v != "":
            return v
    return default

def _env_required(keys: list[str], *, normalize_path: bool = False) -> str:
    v = _env_first(keys)
    if not v:
        joined = ", ".join(keys)
        raise RuntimeError(
            f"필수 환경변수 누락: {joined}\n"
            f"→ .env에 다음 중 하나를 설정하세요. (예시는 값 형식만 참고)\n"
            f"   {keys[0]}=YOUR_VALUE"
        )
    if normalize_path:
        v = str(Path(os.path.expandvars(os.path.expanduser(v))).resolve())
    return v

# ─── Django 기본 ─────────────────────────────────────────────────────────────
SECRET_KEY = _env_required(["SECRET_KEY", "DJANGO_SECRET_KEY"])

# ✅ 기본값을 운영 친화적으로: DEBUG=0
DEBUG = (_env_first(["DJANGO_DEBUG"], default="0") or "0").lower() not in ("0", "false", "no")

# ✅ 기본값을 로컬 전용으로: 127.0.0.1,localhost
ALLOWED_HOSTS = [
    x.strip()
    for x in (
        _env_first(["ALLOWED_HOSTS"], default="127.0.0.1,localhost")
        or "127.0.0.1,localhost"
    ).split(",")
    if x.strip()
]

STATIC_VERSION = os.environ.get("STATIC_VERSION", "dev")

INSTALLED_APPS = [
    "daphne",   # ✅ runserver 를 ASGI(daphne) 기반으로
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "ragapp",
    "channels",     # ✅ 실시간
    "ragapp.livechat",   # app 이름을 이렇게 만들었다면
]

ASGI_APPLICATION = "ragsite.asgi.application"

# 개발 기본(InMemory). 운영은 channels_redis로 바꾸면 됨.
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
        # 운영 예시:
        # "BACKEND": "channels_redis.core.RedisChannelLayer",
        # "CONFIG": {"hosts": [("127.0.0.1", 6379)]},
    }
}

import os as _os
if _os.environ.get("REDIS_URL"):
    CHANNEL_LAYERS["default"] = {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [_os.environ["REDIS_URL"]]},
    }

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # ✅ 정적 파일(ASGI/개발/운영 겸용) — WhiteNoise
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "ragapp.middleware.legal_noindex.LegalSecurityHeadersMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # 검색엔진 크롤 방지 헤더/메타 (법적 베타 단계)
    "ragapp.middleware.legal_noindex.NoIndexMiddleware",
]

ROOT_URLCONF = "ragsite.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "ragapp.context_processors.legal_context",  # ★ 유지
            ],
        },
    },
]
TEMPLATES[0]["OPTIONS"]["context_processors"].append(
    "ragapp.context_processors.static_version"
)

WSGI_APPLICATION = "ragsite.wsgi.application"

# ✅ 벡터 SQLite DB 경로: 환경변수 우선 + 안전한 정규화
#    기본값: BASE_DIR/sqlite3/vector_store.sqlite3
_vector_db_raw = _env_first(
    ["VECTOR_DB_PATH"],
    default=str(BASE_DIR / "sqlite3" / "vector_store.sqlite3"),
) or str(BASE_DIR / "sqlite3" / "vector_store.sqlite3")
VECTOR_DB_PATH = str(
    Path(os.path.expandvars(os.path.expanduser(_vector_db_raw))).resolve()
)
# 필요 시 다른 모듈에서 환경변수로도 재사용할 수 있게 브리지
os.environ.setdefault("VECTOR_DB_PATH", VECTOR_DB_PATH)

# ─── DB ──────────────────────────────────────────────────────────────────────
DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}
}

# ─── 국제화/시간대 ────────────────────────────────────────────────────────────
LANGUAGE_CODE = "ko-kr"
TIME_ZONE = "Asia/Seoul"
USE_I18N = True
USE_TZ = True

# ─── 정적파일 (ASGI/개발/운영 공통) ─────────────────────────────────────────
STATIC_URL = "/static/"
STATICFILES_DIRS = [
    p for p in [
        (BASE_DIR / "static") if (BASE_DIR / "static").exists() else None,
        (BASE_DIR / "ragapp" / "static") if (BASE_DIR / "ragapp" / "static").exists() else None,
    ] if p
]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
}
WHITENOISE_USE_FINDERS = True if DEBUG else False
WHITENOISE_AUTOREFRESH = True if DEBUG else False
WHITENOISE_MAX_AGE = 0 if DEBUG else 60 * 60 * 24 * 365
STATICFILES_FINDERS = [
    "django.contrib.staticfiles.finders.FileSystemFinder",
    "django.contrib.staticfiles.finders.AppDirectoriesFinder",
]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ✅ 미디어(업로드) 파일
MEDIA_URL  = "/uploads/"
MEDIA_ROOT = BASE_DIR / "uploads"
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)

# ─── 경로 유틸 ───────────────────────────────────────────────────────────────
def _canon(p: str | os.PathLike | None) -> str:
    """
    경로 정규화 유틸.
    - 값이 없으면 BASE_DIR / "chroma" 사용
    - 제어문자(ASCII < 32)가 포함되면 기본 경로로 되돌림
      → C:\\x0bscode\\project 같은 잘못된 경로 방지
    """
    if not p:
        candidate = str(BASE_DIR / "chroma")
    else:
        candidate = str(p)

    candidate = os.path.expandvars(os.path.expanduser(candidate))

    # C:\x0bscode\project 같은 제어문자(예: \x0b) 포함 시 안전한 기본값으로 교체
    if any(ord(ch) < 32 for ch in candidate):
        candidate = str(BASE_DIR / "chroma")

    return str(Path(candidate).resolve())

# ─── Chroma / RAG 기본 ──────────────────────────────────────────────────────
# 기본값: BASE_DIR/chroma  (예: C:\vscode\project\chroma)
CHROMA_DB_DIR = _canon(_env_first(["CHROMA_DB_DIR"], default=str(BASE_DIR / "chroma")))
Path(CHROMA_DB_DIR).mkdir(parents=True, exist_ok=True)
CHROMA_COLLECTION = _env_first(["CHROMA_COLLECTION"], default="my_notes")

# --- Vertex/Gemini API key (optional; ADC일 땐 없어도 됨)
VERTEX_API_KEY = (
    os.environ.get("VERTEX_API_KEY")
    or os.environ.get("GOOGLE_API_KEY")
    or os.environ.get("GEMINI_API_KEY")
)
if VERTEX_API_KEY and isinstance(VERTEX_API_KEY, str) and VERTEX_API_KEY.strip():
    os.environ.setdefault("API_KEY", VERTEX_API_KEY)
    os.environ.setdefault("GOOGLE_API_KEY", VERTEX_API_KEY)
    os.environ.setdefault("GEMINI_API_KEY", VERTEX_API_KEY)

# - 프로젝트 ID
VERTEX_PROJECT_ID = _env_required(
    ["VERTEX_PROJECT_ID", "vertex_id", "GCP_PROJECT", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"]
)

# - 리전: 기본 us-central1 (Vertex GenAI 권장/호환)
VERTEX_LOCATION = _env_first(["VERTEX_LOCATION", "GCP_LOCATION"], default="us-central1")

# - 서비스 계정 JSON 경로(선택)
_gac = _env_first(["GOOGLE_APPLICATION_CREDENTIALS"], default="") or ""
if _gac:
    _gac = str(Path(os.path.expandvars(os.path.expanduser(_gac))).resolve())
GOOGLE_APPLICATION_CREDENTIALS = _gac

# 환경변수 브리지(google-genai Vertex 경로 고정)
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
if VERTEX_API_KEY and isinstance(VERTEX_API_KEY, str) and VERTEX_API_KEY.strip():
    os.environ.setdefault("API_KEY", VERTEX_API_KEY)
    os.environ.setdefault("GOOGLE_API_KEY", VERTEX_API_KEY)
os.environ.setdefault("VERTEX_PROJECT_ID", VERTEX_PROJECT_ID)
os.environ.setdefault("GCP_PROJECT", VERTEX_PROJECT_ID)
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", VERTEX_PROJECT_ID)
os.environ.setdefault("GCLOUD_PROJECT", VERTEX_PROJECT_ID)
os.environ.setdefault("VERTEX_LOCATION", VERTEX_LOCATION)
os.environ.setdefault("GCP_LOCATION", VERTEX_LOCATION)
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", VERTEX_LOCATION)
if GOOGLE_APPLICATION_CREDENTIALS:
    os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = GOOGLE_APPLICATION_CREDENTIALS

# (선택) 클라이언트에서 사용할 API 버전 기본값
GENAI_API_VERSION = _env_first(["GENAI_API_VERSION"], default="v1")

# ─── 모델 이름(표시/호출) ────────────────────────────────────────────────────
GEMINI_MODEL = _env_first(
    ["GEMINI_MODEL", "GEMINI_MODEL_DIRECT", "GEMINI_TEXT_MODEL", "VERTEX_TEXT_MODEL"],
    default="gemini-2.0-flash",
)
GEMINI_TEXT_MODEL = _env_first(["GEMINI_TEXT_MODEL"], default=GEMINI_MODEL)
VERTEX_TEXT_MODEL = _env_first(["VERTEX_TEXT_MODEL"], default=GEMINI_MODEL)
GEMINI_MODEL_DIRECT = _env_first(["GEMINI_MODEL_DIRECT"], default=GEMINI_MODEL)
GEMINI_MODEL_RAG    = _env_first(["GEMINI_MODEL_RAG"],    default=GEMINI_MODEL)

# 임베딩 모델
_embed_src = _env_first(["EMBED_MODEL", "GEMINI_EMBED_MODEL", "GEMINI_EMBED_MODELS"], default="text-embedding-004") or ""
GEMINI_EMBED_MODELS = [m.strip() for m in _embed_src.split(",") if m.strip()]
VERTEX_EMBED_MODEL  = _env_first(
    ["VERTEX_EMBED_MODEL", "EMBED_MODEL", "GEMINI_EMBED_MODEL"],
    default=(GEMINI_EMBED_MODELS[0] if GEMINI_EMBED_MODELS else "text-embedding-004"),
)

# ── 인덱싱 / 크롤 옵션 ─────────────────────────────────────
WEB_INGEST_TO_CHROMA      = (_env_first(["WEB_INGEST_TO_CHROMA"], default="1") or "1").lower() not in ("0", "false", "no")
AUTO_INGEST_AFTER_GEMINI  = (_env_first(["AUTO_INGEST_AFTER_GEMINI"], default="1") or "1").lower() not in ("0", "false", "no")
CRAWL_ANSWER_LINKS        = (_env_first(["CRAWL_ANSWER_LINKS"], default="1") or "1").lower() not in ("0", "false", "no")
ANSWER_LINK_MAX           = int(_env_first(["ANSWER_LINK_MAX"], default="5") or "5")
ANSWER_LINK_TIMEOUT       = int(_env_first(["ANSWER_LINK_TIMEOUT"], default="12") or "12")
MIN_NEWS_BODY_CHARS       = int(_env_first(["MIN_NEWS_BODY_CHARS"], default="400") or "400")
EMBED_CHUNK_SIZE          = int(_env_first(["EMBED_CHUNK_SIZE"], default="1600") or "1600")
EMBED_CHUNK_OVERLAP       = int(_env_first(["EMBED_CHUNK_OVERLAP"], default="200") or "200")
NEWS_TOPK                 = int(_env_first(["NEWS_TOPK"], default="5") or "5")

# ── RAG 검색 옵션 ──────────────────────────────────────────
RAG_FORCE_ANSWER   = (_env_first(["RAG_FORCE_ANSWER"], default="1") or "1").lower() not in ("0", "false", "no")
RAG_QUERY_TOPK     = int(_env_first(["RAG_QUERY_TOPK"], default="5") or "5")
RAG_FALLBACK_TOPK  = int(_env_first(["RAG_FALLBACK_TOPK"], default="12") or "12")
RAG_MAX_SOURCES    = int(_env_first(["RAG_MAX_SOURCES"], default="8") or "8")
RAG_SOURCES_FILTER = _env_first(["RAG_SOURCES_FILTER"], default="answer_link,news")

# ── RSS 템플릿 등 기타 설정 ─────────────────────────────────
NEWS_RSS_QUERY_TEMPLATE = _env_first(
    ["NEWS_RSS_QUERY_TEMPLATE"],
    default="https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko",
)
USE_HEADLESS_BROWSER = True
HEADLESS_TIMEOUT_SEC = int(_env_first(["HEADLESS_TIMEOUT_SEC"], default="12") or "12")

# ─── 저작권/컴플라이언스 안전 모드 ─────────────────────────────────────────
SAFE_MODE_ENABLED     = (_env_first(["SAFE_MODE_ENABLED"], default="1") or "1").lower() not in ("0", "false", "no")
SAFE_ROBOTS_RESPECT   = (_env_first(["SAFE_ROBOTS_RESPECT"], default="1") or "1").lower() not in ("0", "false", "no")
SAFE_SUMMARY_ONLY     = (_env_first(["SAFE_SUMMARY_ONLY"], default="1") or "1").lower() not in ("0", "false", "no")
SAFE_USE_LLM_SUMMARY  = (_env_first(["SAFE_USE_LLM_SUMMARY"], default="1") or "1").lower() not in ("0", "false", "no")
SAFE_MIN_BODY_CHARS   = int(_env_first(["SAFE_MIN_BODY_CHARS"], default="600") or "600")
SAFE_STRICT_DELETE    = (_env_first(["SAFE_STRICT_DELETE"], default="1") or "1").lower() not in ("0", "false", "no")

# ★ privacy_page에서 참조하는 플래그 보강
STORE_FULLTEXT       = (_env_first(["STORE_FULLTEXT"], default="0") or "0").lower() not in ("0", "false", "no")

# ─── 크롤 정책(법적/운영) ───────────────────────────────────────────────────
RESPECT_ROBOTS            = (_env_first(["RESPECT_ROBOTS", "SAFE_ROBOTS_RESPECT"], default="1") or "1").lower() not in ("0", "false", "no")
ROBOTS_RESPECT            = (_env_first(["ROBOTS_RESPECT"], default="") or "").lower()
ROBOTS_RESPECT            = RESPECT_ROBOTS if ROBOTS_RESPECT == "" else (ROBOTS_RESPECT not in ("0", "false", "no"))

CRAWL_RATE_LIMIT_PER_HOST = float(_env_first(["CRAWL_RATE_LIMIT_PER_HOST"], default="1") or "1")
CRAWL_PER_HOST_RPS        = float(_env_first(["CRAWL_PER_HOST_RPS"], default=str(CRAWL_RATE_LIMIT_PER_HOST)) or CRAWL_RATE_LIMIT_PER_HOST)
CRAWL_USER_AGENT          = _env_first(["CRAWL_USER_AGENT"], default="ragapp-bot/1.0 (+contact@example.com)")

ALLOWLIST_DOMAINS = [
    x.strip()
    for x in (_env_first(["ALLOWLIST_DOMAINS"], default="") or "").split(",")
    if x.strip()
]
ALLOWED_NEWS_DOMAINS = [
    x.strip()
    for x in (_env_first(["ALLOWED_NEWS_DOMAINS"], default="") or "").split(",")
    if x.strip()
]

# ─── 개인정보 최소화 / 보관 정책 ────────────────────────────────────────────
# ✅ 기본값을 "해시 ON" 으로
LOG_IP_HASHED       = (_env_first(["LOG_IP_HASHED"], default="1") or "1").lower() not in ("0", "false", "no")
LOG_IP_HASH_SECRET  = _env_first(["LOG_IP_HASH_SECRET"], default="") or ""

# ✅ "기본 보존 기간" (모델들이 fallback으로 쓰는 공통값)
# - models.py의 _retention_days(..., fallback=RETENTION_DAYS) 와 연동됨
# - .env에서 RETENTION_DAYS=0 으로 두면 자동삭제 스케줄만 만들어두고 실제 delete_at 계산은 안 하게 할 수도 있음
RETENTION_DAYS      = int(_env_first(["RETENTION_DAYS", "RETENTION_DAYS_DEFAULT"], default="30") or "30")
RETENTION_DAYS_DEFAULT = RETENTION_DAYS  # 별칭(가독성)

LOG_RETENTION_DAYS  = int(_env_first(["LOG_RETENTION_DAYS"], default="30") or "30")
ANONYMIZE_IP        = (_env_first(["ANONYMIZE_IP"], default="1") or "1").lower() not in ("0", "false", "no")

# ★ 테이블별 보존 기간 (없으면 위 RETENTION_DAYS로 fallback)
RETENTION_DAYS_CHATLOG        = int(_env_first(["RETENTION_DAYS_CHATLOG"],        default="90")  or "90")
RETENTION_DAYS_FEEDBACK       = int(_env_first(["RETENTION_DAYS_FEEDBACK"],       default="180") or "180")
RETENTION_DAYS_QARAG_FEEDBACK = int(_env_first(["RETENTION_DAYS_QARAG_FEEDBACK"], default=str(RETENTION_DAYS_FEEDBACK)) or str(RETENTION_DAYS_FEEDBACK))
RETENTION_DAYS_CONSENT        = int(_env_first(["RETENTION_DAYS_CONSENT"],        default="365") or "365")

# 추가 로그/운영 테이블(원하면 여기만 줄여도 됨)
RETENTION_DAYS_APPLOG         = int(_env_first(["RETENTION_DAYS_APPLOG"],         default=str(LOG_RETENTION_DAYS)) or str(LOG_RETENTION_DAYS))
RETENTION_DAYS_MYLOG          = int(_env_first(["RETENTION_DAYS_MYLOG"],          default="90") or "90")
RETENTION_DAYS_INGESTHISTORY  = int(_env_first(["RETENTION_DAYS_INGESTHISTORY"],  default="180") or "180")
RETENTION_DAYS_LIVECHAT       = int(_env_first(["RETENTION_DAYS_LIVECHAT"],       default="180") or "180")

# ✅ 자동삭제(정기 파기) 대상 allowlist
# - "절대 자동삭제 금지"는 allowlist에 넣지 않으면 됨 (예: RagChunk, LegalDocumentVersion, RagSetting 등)
# - 모델 라벨은 "app_label.ModelName" 문자열
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
AUTO_PURGE_MODEL_ALLOWLIST = [
    x.strip()
    for x in (_env_first(["AUTO_PURGE_MODEL_ALLOWLIST"], default=",".join(_default_allowlist)) or "").split(",")
    if x.strip()
]

# (선택) 파기 실행 스위치/옵션 (명령/배치에서 읽어 쓰기)
AUTO_PURGE_ENABLED = (_env_first(["AUTO_PURGE_ENABLED"], default="0") or "0").lower() not in ("0", "false", "no")
AUTO_PURGE_BATCH_SIZE = int(_env_first(["AUTO_PURGE_BATCH_SIZE"], default="1000") or "1000")

# (선택) 모델별 기본 보존기간 매핑(명령/배치에서 활용용)
AUTO_PURGE_RETENTION_BY_MODEL = {
    "ragapp.ChatQueryLog": RETENTION_DAYS_CHATLOG,
    "ragapp.Feedback": RETENTION_DAYS_FEEDBACK,
    "ragapp.FeedbackLog": RETENTION_DAYS_FEEDBACK,
    "ragapp.QaragFeedback": RETENTION_DAYS_QARAG_FEEDBACK,
    "ragapp.MyLog": RETENTION_DAYS_MYLOG,
    "ragapp.IngestHistory": RETENTION_DAYS_INGESTHISTORY,
    "ragapp.AppLog": RETENTION_DAYS_APPLOG,
    "ragapp.LiveChatSession": RETENTION_DAYS_LIVECHAT,
    "ragapp.LiveChatRoom": RETENTION_DAYS_LIVECHAT,
}

# ─── 법적 페이지/연락처(여기 보강!) ──────────────────────────────────────────
CONTACT_EMAIL       = _env_first(["CONTACT_EMAIL", "ADMIN_CONTACT_EMAIL", "SUPPORT_EMAIL"], default="contact@example.com")
LEGAL_DOCS_VERSION  = _env_first(["LEGAL_DOCS_VERSION"], default="v1.0")
LEGAL_EFFECTIVE_DATE= _env_first(["LEGAL_EFFECTIVE_DATE"], default="2025-11-12")

# 정책/고지 페이지(선택) — 미니 안내(/legal/privacy-min/)의 “자세히 보기”가 항상 열리도록 기본값을 정식 문서로 지정
PRIVACY_PAGE_URL    = _env_first(["PRIVACY_PAGE_URL"], default="/legal/privacy/") or "/legal/privacy/"
TERMS_PAGE_URL      = _env_first(["TERMS_PAGE_URL"], default="") or ""
COPYRIGHT_PAGE_URL  = _env_first(["COPYRIGHT_PAGE_URL"], default="") or ""
SITEMAP_URL         = _env_first(["SITEMAP_URL"], default="") or ""

# 검색엔진 차단 배너/헤더 스위치(테스트 기간 권장)
NOINDEX_ENABLED     = (_env_first(["NOINDEX_ENABLED"], default="1") or "1").lower() not in ("0", "false", "no")

# Vertex/Gemini 보관 최소화 & 그라운딩 비활성화
VERTEX_ZERO_DATA_RETENTION = (_env_first(["VERTEX_ZERO_DATA_RETENTION"], default="0") or "0").lower() not in ("0", "false", "no")
VERTEX_DISABLE_GROUNDING   = (_env_first(["VERTEX_DISABLE_GROUNDING"], default="0") or "0").lower() not in ("0", "false", "no")

# ─── Consent / Legal (프런트/서버 공통 버전 문자열) ─────────────────────────
CONSENT_VERSION = _env_first(["CONSENT_VERSION"], default="v2025-11-02")

# ─── 기본 보안 ───────────────────────────────────────────────────────────────
SECURE_SSL_REDIRECT   = (_env_first(["SECURE_SSL_REDIRECT"], default="0") or "0").lower() not in ("0", "false", "no")
SESSION_COOKIE_SECURE = (_env_first(["SESSION_COOKIE_SECURE"], default="0") or "0").lower() not in ("0", "false", "no")
CSRF_COOKIE_SECURE    = (_env_first(["CSRF_COOKIE_SECURE"], default="0") or "0").lower() not in ("0", "false", "no")
SECURE_HSTS_SECONDS   = int(_env_first(["SECURE_HSTS_SECONDS"], default="0") or "0")
SECURE_HSTS_INCLUDE_SUBDOMAINS = (_env_first(["SECURE_HSTS_INCLUDE_SUBDOMAINS"], default="0") or "0").lower() not in ("0", "false", "no")
SECURE_HSTS_PRELOAD   = (_env_first(["SECURE_HSTS_PRELOAD"], default="0") or "0").lower() not in ("0", "false", "no")
SECURE_REFERRER_POLICY = _env_first(["SECURE_REFERRER_POLICY"], default="strict-origin-when-cross-origin")

# ★ CSRF 신뢰 오리진(로컬 개발 기본값 포함)
CSRF_TRUSTED_ORIGINS = [
    x.strip() for x in (
        _env_first(["CSRF_TRUSTED_ORIGINS"], default="http://127.0.0.1:8000,http://localhost:8000") or ""
    ).split(",") if x.strip()
]

# ★ SameSite 기본값
SESSION_COOKIE_SAMESITE = _env_first(["SESSION_COOKIE_SAMESITE"], default="Lax")
CSRF_COOKIE_SAMESITE    = _env_first(["CSRF_COOKIE_SAMESITE"], default="Lax")

# ─── LOGGING ────────────────────────────────────────────────────────────────
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
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
        "db": {
            "class": "ragapp.services.logging_handlers.DBLogHandler",
            "formatter": "verbose",
        },
    },
    "loggers": {
        # ragapp 전체(기본)는 INFO 이상 콘솔+DB
        "ragapp": {
            "handlers": ["console", "db"],
            "level": "INFO",
            "propagate": False,
        },

        # 뉴스 서비스 디버그는 그대로 유지 + DB에도 남기기
        "ragapp.services.news_services": {
            "handlers": ["console", "db"],
            "level": "DEBUG",
            "propagate": False,
        },
        # news_views 모듈(__name__ = "ragapp.news_views.news_views")용 로거
        "ragapp.news_views.news_views": {
            "handlers": ["console", "db"],
            "level": "DEBUG",
            "propagate": False,
        },

        # 장고 자체 로그는 그대로 콘솔만
        "django": {
            "handlers": ["console"],
            "level": "INFO",
        },
    },
}

# 법적 noindex 미들웨어 스위치 (템플릿/미들웨어 공통 플래그)
LEGAL_NOINDEX = True
VDB_MIRROR_TO_RAGCHUNK = True
RAGCHUNK_MIRROR_ENABLED = True
RAGCHUNK_MIRROR_BATCH = 500

AUTO_PURGE_ENABLED = True

AUTO_PURGE_ALLOWLIST = [
  "MyLog", "ChatQueryLog",
  "Feedback", "QaragFeedback", "WebFeedback", "RagSearchFeedback",
  "FeedbackLog", "FeedbackReview",
  "IngestHistory", "LiveChatSession", "AppLog",
]
AUTO_PURGE_NEVER = ["RagChunk", "LegalConfig", "RagSetting", "FaqEntry", "TableSchema", "TableSearchRule"]

AUTO_PURGE_RETENTION_DAYS = {
  "MyLog": 30,
  "AppLog": 30,
  "ChatQueryLog": 90,
  "Feedback": 180,
  "QaragFeedback": 180,
  "WebFeedback": 180,
  "RagSearchFeedback": 180,
  "FeedbackLog": 180,
  "FeedbackReview": 180,
  "IngestHistory": 30,
  "LiveChatSession": 90,
}

# ----------------chat manner------------------
import os as _os_chat

CHAT_RETENTION_DAYS = int(_os_chat.getenv("CHAT_RETENTION_DAYS", "30"))

# “법적 최고 보관 가능일”은 케이스/정책마다 달라서 숫자 하드코딩 말고 env로
ABUSE_RETENTION_DAYS = int(_os_chat.getenv("ABUSE_RETENTION_DAYS", "365"))  # 예: 1년
LEGAL_HOLD_RETENTION_DAYS = int(_os_chat.getenv("LEGAL_HOLD_RETENTION_DAYS", "1825"))  # 예: 5년

# 욕설 키워드(초기버전). 운영하면서 조정하거나, 나중에 모델/모더레이션으로 대체
LIVECHAT_ABUSIVE_KEYWORDS = ["ㅅㅂ", "씨발", "병신", "좆", "ㅈㄹ"]  # 필요시 커스텀

EVIDENCE_ADMIN_GROUP = _os_chat.getenv("EVIDENCE_ADMIN_GROUP", "evidence_admin")
LIVECHAT_ABUSE_RETENTION_DAYS = 365  # “법적 최대”는 니 정책/상황에 맞게 숫자로만 설정해두는 게 안전
LIVECHAT_RETENTION_DAYS = 30
