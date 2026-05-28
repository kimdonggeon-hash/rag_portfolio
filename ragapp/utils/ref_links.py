# ragapp/utils/ref_links.py
from __future__ import annotations

import html
import re
from urllib.parse import (
    urlparse, urlsplit, urlunsplit, parse_qsl, urlencode, unquote
)

import requests
from django.core.cache import cache

SAFE_SCHEMES = {"http", "https"}

# ✅ SSRF 방지: "해석(리다이렉트 따라가기)"는 구글뉴스 계열만 허용
ALLOWED_RESOLVE_HOSTS = {"news.google.com", "www.google.com", "google.com"}

# meta refresh 대응 (가끔 200 HTML로 내려오고 meta refresh로 넘김)
META_REFRESH_RE = re.compile(
    r'http-equiv=["\']refresh["\'][^>]*content=["\'][^;]+;\s*url=([^"\']+)["\']',
    re.I,
)

TRACKING_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "oc", "ved", "cid", "ei", "sa", "source", "opi", "usg",
}

def _safe_http_url(url: str) -> str | None:
    if not url:
        return None
    url = url.strip()
    p = urlparse(url)
    if p.scheme not in SAFE_SCHEMES:
        return None
    if not p.netloc:
        return None
    return url

def _strip_tracking(url: str) -> str:
    try:
        sp = urlsplit(url)
        q = []
        for k, v in parse_qsl(sp.query, keep_blank_values=True):
            if k in TRACKING_KEYS:
                continue
            if k.startswith("utm_"):
                continue
            q.append((k, v))
        return urlunsplit((sp.scheme, sp.netloc, sp.path, urlencode(q, doseq=True), sp.fragment))
    except Exception:
        return url

def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""

def resolve_final_url(url: str, *, timeout=(2, 5), max_redirects=5) -> str:
    safe = _safe_http_url(url)
    if not safe:
        return url

    host = urlparse(safe).netloc.lower()
    # ✅ 해석 대상이 아니면 그대로(=외부 임의 URL을 서버가 fetch 하지 않음)
    if host not in ALLOWED_RESOLVE_HOSTS:
        return _strip_tracking(safe)

    cache_key = f"refurl:v1:{safe}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    try:
        sess = requests.Session()
        sess.max_redirects = max_redirects

        resp = sess.get(
            safe,
            allow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; RAGPortfolioBot/1.0)"},
        )
        final = str(resp.url)

        # meta refresh fallback (구글뉴스가 HTML로 주고 meta refresh로 넘기는 케이스)
        if urlparse(final).netloc.lower() in ALLOWED_RESOLVE_HOSTS:
            m = META_REFRESH_RE.search(resp.text or "")
            if m:
                cand = html.unescape(m.group(1)).strip()
                cand = unquote(cand)
                if _safe_http_url(cand):
                    final = cand

        final = _safe_http_url(final) or safe
        final = _strip_tracking(final)

        cache.set(cache_key, final, 60 * 60 * 24 * 7)  # 7일 캐시
        return final
    except Exception:
        return _strip_tracking(safe)

def normalize_references(refs: list[dict] | None, *, limit=6, resolve=True) -> list[dict]:
    """
    입력 예시 refs:
      [{"title": "...", "url": "..."}, {"name": "...", "link": "..."}] 등
    출력:
      [{"title": "...", "url": "...", "domain": "..."}]
    """
    out: list[dict] = []
    for r in (refs or [])[:limit]:
        title = (r.get("title") or r.get("name") or "").strip()
        raw = (r.get("url") or r.get("link") or "").strip()
        if not raw:
            continue

        href = resolve_final_url(raw) if resolve else raw
        href = _safe_http_url(href) or raw

        out.append({
            "title": title or _host(href) or "참고 링크",
            "url": href,
            "domain": _host(href),
        })
    return out
