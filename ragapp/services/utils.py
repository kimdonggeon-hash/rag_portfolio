# ragapp/services/utils.py
from __future__ import annotations

import re
import hashlib
import hmac
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, urlunparse

from django.conf import settings


# ─────────────────────────────────────────────────────────────
# URL 추출용 정규식들
# ─────────────────────────────────────────────────────────────
_LINK_RE = re.compile(r"https?://[^\s\]\)]+", re.IGNORECASE)
_URL_MD = re.compile(r"\[[^\]]+\]\((https?://[^\s)]+)\)")
_URL_RAW = re.compile(r"(https?://[^\s<>\]\)\"']+)")

# 현실적인 상한(이상치/폭탄 방지)
_MAX_URL_LEN = int(getattr(settings, "MAX_URL_LEN", 2048) or 2048)
_MAX_LINKS_DEFAULT = int(getattr(settings, "MAX_EXTRACT_LINKS", 6) or 6)


def _normalize_url(u: str) -> str:
    """
    URL을 최소한으로 정규화.
    - 공백/마침표 등 트레일링 문장부호 제거
    - http/https만 허용
    - 길이 제한
    - fragment(#...) 제거(중복 감소)
    """
    if not u:
        return ""

    u = (u or "").strip().rstrip(").,];\"'")
    if not u.lower().startswith(("http://", "https://")):
        return ""
    if len(u) > _MAX_URL_LEN:
        return ""

    try:
        p = urlparse(u)
        if p.scheme not in ("http", "https"):
            return ""
        # fragment 제거
        p2 = p._replace(fragment="")
        u2 = urlunparse(p2)
        # 너무 긴 쿼리는 그대로 두되 길이 제한은 이미 걸려 있음
        return u2
    except Exception:
        return ""


def extract_links_from_text(text: str, max_n: int = _MAX_LINKS_DEFAULT):
    """텍스트 안에서 URL만 뽑아 상위 max_n개 리턴."""
    max_n = max(0, int(max_n or 0))
    urls, seen = [], set()
    for m in _LINK_RE.finditer(text or ""):
        u = _normalize_url(m.group(0))
        if not u:
            continue
        if u not in seen:
            urls.append(u)
            seen.add(u)
        if max_n and len(urls) >= max_n:
            break
    return urls


def extract_urls_from_text(text: str, max_n: int | None = None):
    """
    마크다운 형태/생짜 URL 다 긁어서 중복 제거.
    - http/https만
    - 길이 제한
    - fragment 제거
    """
    if not text:
        return []

    urls: list[str] = []
    try:
        urls += _URL_MD.findall(text)
    except Exception:
        pass
    try:
        urls += _URL_RAW.findall(text)
    except Exception:
        pass

    out: list[str] = []
    seen: set[str] = set()
    lim = None if max_n is None else max(0, int(max_n or 0))

    for u in urls:
        u2 = _normalize_url(u)
        if not u2:
            continue
        if u2 in seen:
            continue
        seen.add(u2)
        out.append(u2)
        if lim is not None and lim and len(out) >= lim:
            break
    return out


def slug(s: str, n: int = 60) -> str:
    """한글 포함 문자열 -> 파일/ID로 쓰기 안전한 짧은 슬러그."""
    n = max(1, int(n or 1))
    s = re.sub(r"[^0-9A-Za-z가-힣\-_. ]+", "", s or "")
    s = re.sub(r"\s+", "-", s).strip("-")
    return (s[:n] or "doc").strip("-") or "doc"


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

    size = max(1, int(size or 1))
    overlap = max(0, int(overlap or 0))
    # ✅ 무한루프 방지
    if overlap >= size:
        overlap = size - 1

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

    ✅ 개선:
    - TRUST_X_FORWARDED_FOR 설정으로 XFF 신뢰 여부 제어(기본 True)
    """
    trust_xff = bool(getattr(settings, "TRUST_X_FORWARDED_FOR", True))
    if trust_xff:
        xff = request.META.get("HTTP_X_FORWARDED_FOR")
        if xff:
            return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


# ─────────────────────────────────────────────────────────
# 개인정보 최소화용 IP 해시 로거
# settings.LOG_IP_HASHED / LOG_IP_HASH_SECRET 사용
# ─────────────────────────────────────────────────────────
def _hash_ip(ip: str, secret: str | None) -> str:
    if not ip:
        return ""
    try:
        if secret:
            digest = hmac.new(secret.encode("utf-8"), ip.encode("utf-8"), hashlib.sha256).hexdigest()
            return f"iphash:{digest[:16]}"
        # 시크릿이 없을 때의 폴백(권장: 반드시 시크릿 설정)
        digest = hashlib.sha1(ip.encode("utf-8", "ignore")).hexdigest()
        # ✅ 약한 폴백임을 표시(운영에서 탐지/개선 쉽게)
        return f"iphash_weak:{digest[:16]}"
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
