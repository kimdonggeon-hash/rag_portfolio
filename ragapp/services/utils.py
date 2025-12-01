# ragapp/services/utils.py
import re
import hashlib
import hmac
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from django.conf import settings

# URL 추출용 정규식들
_LINK_RE = re.compile(r"https?://[^\s\]\)]+", re.IGNORECASE)
_URL_MD = re.compile(r"\[[^\]]+\]\((https?://[^\s)]+)\)")
_URL_RAW = re.compile(r"(https?://[^\s<>\]\)\"']+)")

def extract_links_from_text(text: str, max_n: int = 6):
    """텍스트 안에서 URL만 뽑아 상위 max_n개 리턴."""
    urls, seen = [], set()
    for m in _LINK_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,);")
        if u not in seen:
            urls.append(u); seen.add(u)
        if len(urls) >= max_n:
            break
    return urls

def extract_urls_from_text(text: str):
    """마크다운 형태/생짜 URL 다 긁어서 중복 제거."""
    if not text:
        return []
    urls = []
    try:
        urls += _URL_MD.findall(text)
    except Exception:
        pass
    try:
        urls += _URL_RAW.findall(text)
    except Exception:
        pass
    out = []
    seen = set()
    for u in urls:
        u = u.strip().rstrip(").,]")
        if not u.lower().startswith(("http://", "https://")):
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out

def slug(s: str, n: int = 60) -> str:
    """한글 포함 문자열 -> 파일/ID로 쓰기 안전한 짧은 슬러그."""
    s = re.sub(r"[^0-9A-Za-z가-힣\-_. ]+", "", s or "")
    s = re.sub(r"\s+", "-", s).strip("-")
    return s[:n] or "doc"

def sha(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8", "ignore")).hexdigest()[:16]

def iso(dt) -> str:
    """RSS published 같은 날짜를 ISO 문자열로 정규화."""
    try:
        if isinstance(dt, datetime):
            return dt.isoformat()
        if not dt:
            return ""
        try:
            return parsedate_to_datetime(dt).isoformat()
        except Exception:
            return datetime.fromisoformat(str(dt).replace("Z", "+00:00")).isoformat()
    except Exception:
        return ""

def chunk_text(text: str, size: int = 1600, overlap: int = 200):
    """text를 size 단위로 겹치게 슬라이스."""
    t = (text or "").strip()
    if not t:
        return []
    out = []
    i = 0
    n = len(t)
    while i < n:
        j = min(i + size, n)
        out.append(t[i:j])
        if j == n:
            break
        i = j - overlap
    return out

def normalize_where_filter(v):
    """
    문자열/리스트/딕셔너리 -> Chroma where(dict) 형태로 통일.
    예: "answer_link,news" -> {"source":{"$in":["answer_link","news"]}}
    """
    if v is None:
        return None
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        s = v.strip()
        parts = [x.strip() for x in s.split(",") if x.strip()]
        if not parts:
            return None
        if len(parts) == 1:
            return {"source": parts[0]}
        return {"source": {"$in": parts}}
    if isinstance(v, (list, tuple, set)):
        vals = [str(x).strip() for x in v if str(x).strip()]
        if not vals:
            return None
        if len(vals) == 1:
            return {"source": vals[0]}
        return {"source": {"$in": vals}}
    return None

def source_label(meta: dict) -> str:
    """템플릿에서 RAG 소스 표시용."""
    title = (meta or {}).get("title") or (meta or {}).get("url") or "문서"
    src = (meta or {}).get("source_name") or (meta or {}).get("source") or (meta or {}).get("publisher") or ""
    txt = f"{title} · {src}".strip(" ·")
    return txt

def get_client_ip(request):
    """
    reverse proxy (X-Forwarded-For) 를 고려해서
    클라이언트 IP 문자열을 뽑아준다. 못 찾으면 None.
    """
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")

# ─────────────────────────────────────────────────────────
# NEW: 개인정보 최소화용 IP 해시 로거
# settings.LOG_IP_HASHED / LOG_IP_HASH_SECRET 사용
# ─────────────────────────────────────────────────────────
def _hash_ip(ip: str, secret: str | None) -> str:
    if not ip:
        return ""
    try:
        if secret:
            digest = hmac.new(secret.encode("utf-8"), ip.encode("utf-8"), hashlib.sha256).hexdigest()
        else:
            # 시크릿이 없을 때의 폴백(권장: 반드시 시크릿 설정)
            digest = hashlib.sha1(ip.encode("utf-8", "ignore")).hexdigest()
        return f"iphash:{digest[:16]}"
    except Exception:
        # 문제가 생겨도 로깅은 진행할 수 있도록 간단 폴백
        return "iphash:unknown"

def client_ip_for_log(request):
    """
    로깅/DB 저장용 IP 문자열을 반환.
    - LOG_IP_HASHED=True 이면 HMAC 해시(비가역) 값으로 대체
    - False 이면 원래 IP 그대로
    """
    ip = get_client_ip(request) or ""
    if not ip:
        return None
    if getattr(settings, "LOG_IP_HASHED", False):
        secret = getattr(settings, "LOG_IP_HASH_SECRET", "") or ""
        return _hash_ip(ip, secret)
    return ip
