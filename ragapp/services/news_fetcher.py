# ragapp/services/news_fetcher.py
from __future__ import annotations

import logging
import time
import re
from datetime import datetime
from typing import List, Dict, Tuple, Optional
from urllib.parse import urlencode, urlparse, urljoin

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from urllib import robotparser as _robotparser

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 기본 설정/헤더
# ─────────────────────────────────────────────────────────────
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)

_DEFAULT_TIMEOUT = int(getattr(settings, "HEADLESS_TIMEOUT_SEC", 12) or 12)
_RESPECT_ROBOTS = bool(getattr(settings, "RESPECT_ROBOTS", True))
_RATE_PER_HOST = float(getattr(settings, "CRAWL_RATE_LIMIT_PER_HOST", 1.0) or 1.0)  # req/sec
_ALLOWLIST = list(getattr(settings, "ALLOWLIST_DOMAINS", []) or [])

_session = requests.Session()
_session.headers.update(
    {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
)

# per-host rate limit state
_LAST_REQ_AT = {}  # host -> timestamp (float, time.time())
_RP_CACHE: dict[str, _robotparser.RobotFileParser] = {}  # host -> robots parser

# ─────────────────────────────────────────────────────────────
# 도메인 화이트리스트
# ─────────────────────────────────────────────────────────────
def _host_allowed(url: str) -> bool:
    if not url:
        return False
    if not _ALLOWLIST:  # 비어 있으면 모두 허용
        return True
    host = (urlparse(url).netloc or "").lower()
    if not host:
        return False
    for d in _ALLOWLIST:
        d = (d or "").lower().strip()
        if not d:
            continue
        if host == d or host.endswith("." + d):
            return True
    return False

# ─────────────────────────────────────────────────────────────
# robots.txt
# ─────────────────────────────────────────────────────────────
def _robots_can_fetch(url: str) -> bool:
    if not _RESPECT_ROBOTS:
        return True
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if not host:
            return False
        rp = _RP_CACHE.get(host)
        if rp is None:
            robots_url = f"{parsed.scheme}://{host}/robots.txt"
            rp = _robotparser.RobotFileParser()
            rp.set_url(robots_url)
            try:
                rp.read()
            except Exception:
                # robots 불가시 보수적으로 허용 (혹은 False로 바꿀 수 있음)
                pass
            _RP_CACHE[host] = rp
        return rp.can_fetch(_UA, url)
    except Exception:
        return True

# ─────────────────────────────────────────────────────────────
# Rate limit per host
# ─────────────────────────────────────────────────────────────
def _rate_limit_wait(url: str) -> None:
    try:
        host = (urlparse(url).netloc or "").lower()
        if not host or _RATE_PER_HOST <= 0:
            return
        min_interval = 1.0 / _RATE_PER_HOST
        last_at = _LAST_REQ_AT.get(host, 0.0)
        now = time.time()
        wait = last_at + min_interval - now
        if wait > 0:
            time.sleep(min(wait, 1.5))
        _LAST_REQ_AT[host] = time.time()
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────
# HTML 파서 유틸
# ─────────────────────────────────────────────────────────────
def _extract_title_and_desc(html: str) -> Tuple[str, str]:
    """
    <title>과 <meta name='description'>, og:description 등에서 요약을 뽑는다.
    반환: (title, description)
    """
    if not html:
        return "", ""
    soup = BeautifulSoup(html, "html.parser")

    # title
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    # description candidates
    desc = ""
    metas = soup.find_all("meta")
    for m in metas:
        name = (m.get("name") or m.get("property") or "").lower()
        if name in ("description", "og:description", "twitter:description"):
            content = (m.get("content") or "").strip()
            if content:
                desc = content
                break

    return title, desc


def _clean_text(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())
    return s


def _extract_main_text(html: str, *, hard_limit: int = 40_000) -> str:
    """
    가벼운 휴리스틱으로 본문 텍스트를 추출.
    (readability 같은 외부 의존성 없이 동작)
    """
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    # 제거: script/style/noscript/nav/footer/aside
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for tag in soup.find_all(["nav", "footer", "aside"]):
        tag.decompose()

    # 후보: article, main, id/class에 article|content|post|story 포함
    candidates = []
    for sel in ["article", "main"]:
        candidates.extend(soup.find_all(sel))

    def _score(node):
        text = _clean_text(node.get_text(" ", strip=True))
        return len(text)

    if not candidates:
        # class/id 휴리스틱
        candidates = [
            *soup.find_all(attrs={"id": re.compile(r"(article|content|post|story)", re.I)}),
            *soup.find_all(attrs={"class": re.compile(r"(article|content|post|story)", re.I)}),
        ]

    if candidates:
        best = max(candidates, key=_score, default=None)
        text = _clean_text(best.get_text(" ", strip=True)) if best else ""
    else:
        # fallback: body 전체
        body = soup.body or soup
        text = _clean_text(body.get_text(" ", strip=True))

    if len(text) > hard_limit:
        text = text[:hard_limit]
    return text

# ─────────────────────────────────────────────────────────────
# HTTP fetch
# ─────────────────────────────────────────────────────────────
def _fetch_html(url: str, *, timeout: int | float) -> tuple[str, str]:
    """
    반환: (final_url, html)
    """
    if not url or not _host_allowed(url):
        return "", ""

    if not _robots_can_fetch(url):
        log.info("robots.txt에 의해 차단: %s", url)
        return "", ""

    _rate_limit_wait(url)

    try:
        resp = _session.get(url, timeout=timeout, allow_redirects=True)
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "text/html" not in ctype and "application/xhtml+xml" not in ctype:
            return resp.url or url, ""

        # 인코딩 보정
        if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or "utf-8"

        html = resp.text or ""
        return resp.url or url, html
    except Exception as e:
        log.warning("fetch 실패: %s (%s)", url, e)
        return "", ""

# ─────────────────────────────────────────────────────────────
# 공개 API
# ─────────────────────────────────────────────────────────────
def search_news_rss(query: str, topk: int = 5) -> List[Dict]:
    """
    Google News RSS 템플릿 기반으로 헤드라인만 가져온다.
    반환 item dict 예시:
      {title, url, source, published_at, snippet}
    """
    tmpl = getattr(
        settings,
        "NEWS_RSS_QUERY_TEMPLATE",
        "https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko",
    )
    url = tmpl.format(query=urlencode({"": query})[1:])  # 'q=...'만 치환
    final, html = _fetch_html(url, timeout=_DEFAULT_TIMEOUT)
    if not html:
        return []

    soup = BeautifulSoup(html, "xml")
    items = soup.find_all("item")[: max(0, topk)]

    out = []
    for it in items:
        title = (it.title.text if it.title else "").strip()
        link = (it.link.text if it.link else "").strip()
        pub = (it.pubDate.text if it.pubDate else "") or (it.find_text("published") or "")
        source = ""
        src_tag = it.find("source")
        if src_tag and src_tag.text:
            source = src_tag.text.strip()
        if not source:
            source = (urlparse(link).netloc or "").lower()

        # desc/snippet
        desc = (it.description.text if it.description else "") or ""
        snippet = BeautifulSoup(desc, "html.parser").get_text(" ", strip=True)
        snippet = _clean_text(snippet)[:300]

        out.append(
            {
                "title": title,
                "url": link,
                "source": source,
                "published_at": pub,
                "snippet": snippet,
            }
        )
    return out


def fetch_article_text(url: str, *, timeout: int | float = None, min_chars: int = 400) -> str:
    """
    단일 기사 URL에서 본문 텍스트를 추출.
    - allowlist/robots/rate-limit 적용
    - text/html 아닌 경우 빈 문자열
    - 본문 길이가 min_chars 미만이면 빈 문자열(노이즈 방지)
    """
    timeout = timeout or _DEFAULT_TIMEOUT
    final, html = _fetch_html(url, timeout=timeout)
    if not html:
        return ""

    text = _extract_main_text(html)
    if len(text) < max(0, int(min_chars or 0)):
        # 메타 설명이라도 반환할지? 저장 로직은 상위에서 결정하므로 여기선 빈값 유지
        return ""
    return text


def crawl_news_bodies(news_headers: List[Dict], *, timeout: int | float = None) -> List[Dict]:
    """
    search_news_rss 결과에 대해 (허용 시) 본문을 추가로 가져온다.
    안전모드/요약저장 여부는 '저장 단계'에서 이미 강제되므로 여기서는 단순 수집만 담당.
    """
    timeout = timeout or _DEFAULT_TIMEOUT
    out = []
    for h in news_headers or []:
        url = (h.get("url") or "").strip()
        if not url or not _host_allowed(url):
            # allowlist 밖이면 아예 스킵
            continue

        final, html = _fetch_html(url, timeout=timeout)
        if not html:
            # 본문 실패시 기본 메타만 유지
            title = h.get("title") or ""
            source = h.get("source") or (urlparse(url).netloc or "")
            pub = h.get("published_at") or ""
            desc = h.get("snippet") or ""
            if not desc:
                _, desc = _extract_title_and_desc(html or "")
            out.append(
                {
                    "title": title,
                    "url": url,
                    "final_url": final or url,
                    "source": source,
                    "published_at": pub,
                    "snippet": desc,
                    "news_body": "",
                }
            )
            continue

        # 요약/본문 추출
        title0, desc0 = _extract_title_and_desc(html)
        main_text = _extract_main_text(html)

        out.append(
            {
                "title": (h.get("title") or title0 or (urlparse(url).netloc or "뉴스")).strip(),
                "url": url,
                "final_url": final or url,
                "source": h.get("source") or (urlparse(url).netloc or ""),
                "published_at": h.get("published_at") or "",
                "snippet": (h.get("snippet") or desc0 or "").strip(),
                "news_body": main_text,
            }
        )
    return out
