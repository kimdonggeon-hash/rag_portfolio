from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, urlparse

import ipaddress
import socket
import re
from urllib.parse import urljoin

import requests
from django.conf import settings

# 선택: bs4 있으면 제목/메타 설명을 좀 더 정확하게 뽑음
try:
    from bs4 import BeautifulSoup  # type: ignore
except Exception:  # pragma: no cover
    BeautifulSoup = None  # type: ignore

# robots.txt 준수
from urllib import robotparser

from ragapp.services.utils import extract_urls_from_text
from ragapp.services.ingest import indexto_chroma_safe

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 환경/상수
# ─────────────────────────────────────────────────────────────
_UA = (
    "Mozilla/5.0 (compatible; RAGNewsBot/1.0; +https://example.local/bot) "
    "ChatClient/QA-RAG"
)

_CRAWL_ENABLED = bool(getattr(settings, "CRAWL_ANSWER_LINKS", True))
_MAX_LINKS = int(getattr(settings, "ANSWER_LINK_MAX", 5))
_TIMEOUT = int(getattr(settings, "ANSWER_LINK_TIMEOUT", 12))

# 안전 옵션(요약만 저장 / 원문 금지)
_SAFE_SUMMARY_ONLY = bool(getattr(settings, "SAFE_SUMMARY_ONLY", True))
_SAFE_MODE_ENABLED = bool(getattr(settings, "SAFE_MODE_ENABLED", True))

# 로봇/도메인 통제
_RESPECT_ROBOTS = bool(getattr(settings, "RESPECT_ROBOTS", True))
_ALLOWLIST = [d.lower() for d in getattr(settings, "ALLOWLIST_DOMAINS", []) or []]
_RATE_PER_HOST = float(getattr(settings, "CRAWL_RATE_LIMIT_PER_HOST", 1.0))  # e.g. 1 req/sec/host

# 스니펫 길이 제한
_MAX_EXCERPT = int(getattr(settings, "MAX_EXCERPT_CHARS", 0) or 0)  # 0이면 내부 디폴트 사용
_SNIPPET_LEN = _MAX_EXCERPT if _MAX_EXCERPT > 0 else 500

# 호스트별 마지막 요청시각(초간단 레이트리밋)
_last_hit: Dict[str, float] = {}

# robots 캐시
_robots_cache: Dict[str, robotparser.RobotFileParser] = {}


# ─────────────────────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────────────────────
def _domain(u: str) -> str:
    # ✅ netloc(포트 포함) 말고 hostname만
    try:
        host = (urlparse(u).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def _is_allowed_domain(u: str) -> bool:
    if not _ALLOWLIST:
        return True
    d = _domain(u)
    return any(d == w or d.endswith("." + w) for w in _ALLOWLIST)


def _is_bad_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
        )
    except Exception:
        return True


def _ssrf_ok(host: str) -> bool:
    """
    ✅ DNS로 해석된 IP들 중 private/local이면 차단
    """
    if not host:
        return False

    # IP literal이면 바로 체크
    try:
        ipaddress.ip_address(host)
        return not _is_bad_ip(host)
    except Exception:
        pass

    try:
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            ip_str = info[4][0]
            if _is_bad_ip(ip_str):
                return False
        return True
    except Exception:
        return False


def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[:n].rstrip() + "…"


def _extract_title_desc_published_canonical(html: str, base_url: str) -> tuple[str, str, str, str]:
    """
    ✅ 메타만: title + (meta description/og:description) + published_time(있으면) + canonical(있으면)
    ❌ 본문 텍스트(get_text) 사용 금지
    """
    if not html:
        return "", "", "", ""

    title = desc = published = canonical = ""

    if BeautifulSoup is None:
        # 최소 파싱(정규식): title/description 정도만
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()

        m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', html, re.I | re.S)
        if not m:
            m = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']', html, re.I | re.S)
        if m:
            desc = re.sub(r"\s+", " ", m.group(1)).strip()

        return title, desc, "", ""

    try:
        soup = BeautifulSoup(html, "html.parser")

        t = soup.find("title")
        if t and t.text:
            title = t.text.strip()

        def _meta(name: str = "", prop: str = "") -> str:
            if name:
                m = soup.find("meta", attrs={"name": name})
                if m and m.get("content"):
                    return m["content"].strip()
            if prop:
                m = soup.find("meta", attrs={"property": prop})
                if m and m.get("content"):
                    return m["content"].strip()
            return ""

        desc = _meta(name="description") or _meta(prop="og:description") or _meta(name="twitter:description")

        published = _meta(prop="article:published_time") or _meta(name="pubdate") or _meta(name="date")

        link = soup.find("link", attrs={"rel": re.compile(r"\bcanonical\b", re.I)})
        if link and link.get("href"):
            canonical = urljoin(base_url, link["href"].strip())

    except Exception:
        pass

    return title, desc, published, canonical



def _read_limited_text(resp: requests.Response, max_bytes: int) -> str:
    buf = bytearray()
    for chunk in resp.iter_content(chunk_size=8192):
        if not chunk:
            continue
        buf.extend(chunk)
        if len(buf) >= max_bytes:
            break
    return buf.decode(resp.encoding or "utf-8", errors="replace")


def _fetch_page(url: str) -> Optional[Dict]:
    # ✅ normalize된 url이 들어온다고 가정하지만, 여기서도 최종 방어
    if not _is_allowed_domain(url):
        return None
    if not _robots_ok(url):
        return None

    # ✅ SSRF 차단
    host = (urlparse(url).hostname or "").lower()
    if not host or not _ssrf_ok(host):
        return None

    _respect_rate_limit(url)

    # ✅ 바이트 제한(설정 없으면 64KB)
    max_bytes = int(getattr(settings, "ANSWER_LINK_MAX_BYTES", 64 * 1024))

    # ✅ 리다이렉트 수동 처리(allowlist/ssrf/robots 재검사)
    max_redirects = int(getattr(settings, "ANSWER_LINK_MAX_REDIRECTS", 3))
    cur = url

    for _ in range(max_redirects + 1):
        chost = (urlparse(cur).hostname or "").lower()
        if not chost or not _is_allowed_domain(cur) or not _ssrf_ok(chost):
            return None
        if not _robots_ok(cur):
            return None

        _respect_rate_limit(cur)

        try:
            r = requests.get(
                cur,
                headers={
                    "User-Agent": _UA,
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
                    "Range": f"bytes=0-{max_bytes-1}",
                },
                timeout=_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
        except Exception as e:
            log.debug("answer-link fetch error: %s (%s)", cur, e)
            return None

        # redirect?
        if 300 <= r.status_code < 400:
            loc = r.headers.get("Location") or ""
            if not loc:
                return None
            cur = urljoin(cur, loc)
            continue

        if r.status_code < 200 or r.status_code >= 300:
            return None

        ctype = (r.headers.get("Content-Type") or "").lower()
        if not (ctype.startswith("text/html") or ctype.startswith("application/xhtml+xml")):
            return None

        html = _read_limited_text(r, max_bytes)

        title, desc, published, canonical = _extract_title_desc_published_canonical(html, cur)

        final_url = canonical or cur
        final_host = _domain(final_url) or _domain(cur)

        # canonical도 보수적으로 검사(allowlist/ssrf)
        if final_host and (_ALLOWLIST and not _is_allowed_domain(final_url)):
            final_url = cur
            final_host = _domain(cur)
        if final_host and not _ssrf_ok(final_host):
            final_url = cur
            final_host = _domain(cur)

        # ✅ 안전규칙: 메타만. 본문/snippet(본문기반) 금지.
        # desc가 없으면 빈 문자열(또는 title) 정도로만.
        safe_desc = _clip(desc or "", _SNIPPET_LEN)

        return {
            "title": _clip(title or final_url, 140),
            "url": final_url,
            "source": final_host,
            "published_at": _clip(published or "", 40),
            "snippet": safe_desc,       # ✅ 메타 description만
            "news_body": "",            # ✅ 본문 저장 금지
            "meta_only": True,
            "bytes_fetched": min(len(html.encode("utf-8", "ignore")), max_bytes),
        }

    return None

def _respect_rate_limit(u: str):
    host = _domain(u)
    if not host or _RATE_PER_HOST <= 0:
        return
    now = time.time()
    last = _last_hit.get(host, 0.0)
    min_interval = 1.0 / _RATE_PER_HOST
    wait = last + min_interval - now
    if wait > 0:
        time.sleep(min(wait, 1.0))
    _last_hit[host] = time.time()

def _robots_ok(u: str) -> bool:
    if not _RESPECT_ROBOTS:
        return True
    try:
        p = urlparse(u)

        host = (p.hostname or "").lower()
        if not host:
            return False
        if not _ssrf_ok(host):
            return False

        # allowlist가 켜져 있으면 robots도 같은 정책으로
        if _ALLOWLIST and (not _is_allowed_domain(u)):
            return False

        netloc = host if not p.port else f"{host}:{p.port}"
        robots_url = f"{p.scheme}://{netloc}/robots.txt"

        rp = _robots_cache.get(robots_url)
        if rp is None:
            try:
                r = requests.get(
                    robots_url,
                    headers={"User-Agent": _UA},
                    timeout=_TIMEOUT,
                    allow_redirects=False,   # ✅ 중요
                )
                txt = r.text if r.status_code == 200 else ""
            except Exception:
                txt = ""
            rp = robotparser.RobotFileParser()
            rp.parse(txt.splitlines())
            _robots_cache[robots_url] = rp

        return rp.can_fetch(_UA, u)

    except Exception:
        return True


# ─────────────────────────────────────────────────────────────
# 공개 API
# ─────────────────────────────────────────────────────────────
_TRACKING_QS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid"
}

def _normalize_url(u: str) -> Tuple[Optional[str], str]:
    u = (u or "").strip()
    if not u:
        return None, "empty"

    try:
        p = urlsplit(u)
        scheme = (p.scheme or "").lower()
        if scheme not in ("http", "https"):
            return None, f"bad_scheme:{scheme or 'none'}"

        host = (p.hostname or "").lower()
        if not host:
            return None, "no_host"

        netloc = host
        if p.port:
            netloc = f"{host}:{p.port}"

        path = p.path or "/"

        # fragment 제거 + (선택) 트래킹 쿼리 제거
        qs = [(k, v) for (k, v) in parse_qsl(p.query, keep_blank_values=True)
              if (k or "").lower() not in _TRACKING_QS]
        query = urlencode(qs, doseq=True)

        norm = urlunsplit((scheme, netloc, path, query, ""))
        return norm, "ok"
    except Exception:
        return None, "parse_error"


def safe_auto_ingest_answer_links(question: str, answer_text: str) -> Dict:
    """
    답변 안의 URL을 추출해 '메타-전용'으로 인덱싱한다.
    - ALLOWLIST_DOMAINS 준수
    - robots.txt 준수(RESPECT_ROBOTS=True일 때)
    - 본문 저장 금지(요약/메타만 저장)
    - 호스트별 레이트리밋 준수
    """
    if not _CRAWL_ENABLED:
        return {"status": "skip", "reason": "CRAWL_ANSWER_LINKS disabled"}

    try:
        raw_urls: List[str] = extract_urls_from_text(answer_text or "")
        if not raw_urls:
            return {"status": "skip", "reason": "no urls found in answer"}

        items: List[Dict] = []
        indexed: List[str] = []
        skipped: List[Dict] = []
        seen = set()

        # ✅ 유니크 기준으로 MAX 채우기
        for raw in raw_urls:
            norm, why = _normalize_url(raw)
            if not norm:
                skipped.append({"url": raw, "reason": why})
                continue

            if norm in seen:
                continue
            seen.add(norm)

            if len(seen) > _MAX_LINKS:
                break

            try:
                # ⚠️ 최종 방어선은 반드시 _fetch_page 내부에서:
                # - allowlist 검사
                # - robots 검사(옵션)
                # - SSRF(IP/사설망) 차단
                # - redirect 제한
                # - timeout/최대바이트/Content-Type 제한
                item = _fetch_page(norm)
                if item:
                    items.append(item)
                    indexed.append(norm)
                else:
                    skipped.append({"url": norm, "reason": "rejected_by_policy_or_fetch_failed"})
            except Exception as e:
                skipped.append({"url": norm, "reason": f"fetch_error:{type(e).__name__}"})

        if not items:
            return {
                "status": "skip",
                "reason": "no eligible urls after policy checks",
                "urls_considered": min(len(raw_urls), _MAX_LINKS),
                "indexed": indexed,
                "skipped": skipped[:50],  # 너무 길어지는 것 방지
            }

        ingest_summary = indexto_chroma_safe(question or "(no question)", "", items)
        return {
            "status": "ok",
            "urls_found": len(raw_urls),
            "urls_indexed": len(items),
            "indexed": indexed,
            "skipped": skipped[:50],
            "ingest_summary": ingest_summary,
        }

    except Exception as e:
        log.debug("safe_auto_ingest_answer_links error: %s", e)
        return {"status": "error", "error": str(e)}