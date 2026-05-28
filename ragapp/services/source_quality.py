# ragapp/services/source_quality.py
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode


# ─────────────────────────────────────────────
# Quality filter with "유두리"
# - Remove boilerplate lines (privacy/cookie/terms/footer/nav)
# - Drop policy-like pages by URL hints (strong)
# - Canonicalize URL (remove fragments + tracking params)
# - Dedupe by canonical URL or title+source
# - Multi-pass fallback: strict → relaxed → last-resort
# ─────────────────────────────────────────────

_BOILER_LINE_PATTERNS = [
    r"개인정보\s*처리\s*방침",
    r"개인정보\s*보호\s*정책",
    r"개인정보\s*취급\s*방침",
    r"개인정보\s*수집\s*및\s*이용",
    r"개인정보\s*제3자\s*제공",
    r"개인정보\s*국외\s*이전",
    r"쿠키\s*(정책|설정|안내)?",
    r"이용\s*약관",
    r"서비스\s*이용약관",
    r"약관",
    r"저작권|copyright|\b©\b|all\s*rights\s*reserved",
    r"문의하기|고객센터|광고\s*문의|제휴\s*문의|채용|recruit",
    r"사이트\s*맵|sitemap",
    r"로그인|회원가입|구독|뉴스레터",
    r"공유하기|공유\s*하기|twitter|facebook|instagram|kakao",
    r"관련기사|기사\s*추천|추천기사|인기기사|많이\s*본\s*기사",
    r"댓글|댓글\s*쓰기|로그인\s*후\s*댓글",
    r"브라우저\s*설정|추적|tracking|analytics",
    r"\bprivacy\s*policy\b",
    r"\bcookie(s)?\b",
    r"\bterms(\s+of\s+service)?\b",
    r"\blegal\b",
    r"\bgdpr\b",
]

_BOILER_LINE_RE = re.compile("|".join(_BOILER_LINE_PATTERNS), re.IGNORECASE)

_POLICY_URL_HINTS = [
    "privacy", "policy", "terms", "cookie", "legal", "gdpr", "consent",
    "개인정보", "처리방침", "이용약관", "쿠키",
]

_TRACKING_PARAMS_PREFIX = ("utm_",)
_TRACKING_PARAMS_EXACT = {
    "gclid", "fbclid", "igshid", "mc_cid", "mc_eid", "yclid",
    "spm", "ref", "ref_src", "cmpid", "cmp", "feature",
}

_NAV_SEPS = ["|", "·", ">", "»", "—", "–"]


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).strip())
    except Exception:
        return None


def canonicalize_url(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    try:
        sp = urlsplit(u)
        q = []
        for k, v in parse_qsl(sp.query, keep_blank_values=True):
            kl = (k or "").strip().lower()
            if not kl:
                continue
            if kl.startswith(_TRACKING_PARAMS_PREFIX):
                continue
            if kl in _TRACKING_PARAMS_EXACT:
                continue
            q.append((k, v))
        q.sort(key=lambda kv: kv[0].lower())
        new_query = urlencode(q, doseq=True)
        return urlunsplit((sp.scheme, sp.netloc, sp.path, new_query, "")).strip()
    except Exception:
        return u.split("#", 1)[0].strip()


def is_policy_like_url(u: str) -> bool:
    ul = (u or "").strip().lower()
    if not ul:
        return False
    return any(h.lower() in ul for h in _POLICY_URL_HINTS)


def _collapse_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def clean_snippet(text: str, *, max_chars: int = 520) -> str:
    t = (text or "").strip()
    if not t:
        return ""

    lines = [ln.strip() for ln in t.splitlines() if ln and ln.strip()]
    kept: List[str] = []

    for ln in lines:
        ln2 = ln.strip()
        if len(ln2) <= 8:
            continue
        if _BOILER_LINE_RE.search(ln2):
            continue
        if len(ln2) < 180:
            sep_cnt = sum(ln2.count(x) for x in _NAV_SEPS)
            if sep_cnt >= 4:
                continue
        if len(ln2) < 140 and ("개인정보" in ln2 or "privacy" in ln2.lower()):
            if "|" in ln2 or ">" in ln2 or "»" in ln2:
                continue

        kept.append(ln2)
        if sum(len(x) for x in kept) >= max_chars:
            break

    out = _collapse_ws("\n".join(kept))
    if len(out) > max_chars:
        out = out[:max_chars].rstrip()
    return out


def _is_pdfish(source_type: str, url: str, title: str) -> bool:
    st = (source_type or "").strip().lower()
    if st in ("pdf", "file", "doc", "document"):
        return True
    # url이 없고 title만 있는 경우(로컬 문서/업로드 문서 느낌)도 완화 대상
    if not (url or "").strip() and (title or "").strip():
        return True
    return False


def looks_like_boilerplate(*, title: str, url: str, snippet: str, source_type: str, min_kept_chars: int) -> bool:
    title = (title or "").strip()
    url = (url or "").strip()
    snippet = (snippet or "").strip()

    pdfish = _is_pdfish(source_type, url, title)

    # 정책/약관 URL은 강하게 컷 (단, pdfish는 예외)
    if url and is_policy_like_url(url) and not pdfish:
        return True

    cleaned = clean_snippet(snippet)

    if not cleaned:
        if _BOILER_LINE_RE.search(title):
            return True
        # 스니펫이 비었는데도 PDFish면 살릴 여지(후단 fallback에서 처리)
        return not pdfish

    if len(cleaned) < min_kept_chars:
        blob = f"{title}\n{snippet}".strip()
        if _BOILER_LINE_RE.search(blob):
            return True
        sep_cnt = sum(cleaned.count(x) for x in _NAV_SEPS)
        if sep_cnt >= 3 and not pdfish:
            return True

    blob2 = f"{title}\n{snippet}".strip()
    hits = len(_BOILER_LINE_RE.findall(blob2))
    if hits >= 3 and not pdfish:
        return True

    return False


def filter_source_cards_dicts(
    items: Iterable[Dict[str, Any]],
    *,
    max_cards: int = 5,
    min_score: float | None = None,
    drop_boilerplate: bool = True,
    dedupe: bool = True,
    ensure_idx: bool = True,
) -> List[Dict[str, Any]]:
    """
    ✅ 유두리 있는 고급 필터
    - 1차 strict: min_snip=70
    - 2차 relaxed: min_snip=40 (짧아도 살림)
    - 3차 last-resort: min_snip=15 + 제목/URL 기반 최소 카드 확보(정책 URL은 계속 컷)
    """

    src_list: List[Dict[str, Any]] = [x for x in (items or []) if isinstance(x, dict)]

    # score 기반 정렬(있으면), 없으면 입력 순서 유지에 가깝게(안정 정렬)
    def _score_key(d: Dict[str, Any]) -> float:
        s = _to_float(d.get("score"))
        return s if s is not None else -1e18

    src_list.sort(key=_score_key, reverse=True)

    def _pass(min_snip_chars: int, *, allow_title_fallback: bool) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen: set[Tuple[str, str, str]] = set()

        for it in src_list:
            title = (it.get("title") or it.get("page_title") or it.get("file_name") or "").strip()
            url_raw = (it.get("url") or it.get("link") or "").strip()
            source_type = (it.get("source_type") or it.get("type") or it.get("source") or "").strip()
            snippet_raw = (
                it.get("snippet")
                or it.get("chunk")
                or it.get("text")
                or it.get("page_content")
                or ""
            )
            snippet_raw = (snippet_raw or "").strip()

            score = _to_float(it.get("score"))

            # score 컷
            if min_score is not None and score is not None and score < float(min_score):
                continue

            # 정책/약관 URL은 어떤 pass에서도 기본 컷(가장 큰 노이즈 원인)
            # 단, pdfish는 예외
            if url_raw and is_policy_like_url(url_raw) and not _is_pdfish(source_type, url_raw, title):
                continue

            snippet_clean = clean_snippet(snippet_raw)

            if drop_boilerplate:
                if looks_like_boilerplate(
                    title=title,
                    url=url_raw,
                    snippet=snippet_raw,
                    source_type=source_type,
                    min_kept_chars=min_snip_chars,
                ):
                    # strict/relaxed에서는 컷
                    # 단, clean이 충분히 남으면 살림
                    if len(snippet_clean) < min_snip_chars:
                        continue

            # 스니펫이 없으면: last-resort에서만 제목으로 대체(유두리)
            if not snippet_clean:
                if allow_title_fallback and title:
                    snippet_clean = title
                else:
                    continue

            # dedupe
            url_can = canonicalize_url(url_raw)
            if dedupe:
                tkey = (title or "").lower()
                skey = (it.get("source") or it.get("source_name") or "").strip().lower()
                ukey = (url_can or "").lower()
                if ukey:
                    key = ("url", ukey, "")
                else:
                    key = ("ts", tkey, skey)
                if key in seen:
                    continue
                seen.add(key)

            new_it = dict(it)
            new_it["title"] = title or new_it.get("title") or "(제목 없음)"
            new_it["url"] = url_can or url_raw or ""
            new_it["snippet"] = snippet_clean
            if score is not None:
                new_it["score"] = score

            out.append(new_it)
            if len(out) >= max_cards:
                break

        return out

    # 1) strict
    out = _pass(70, allow_title_fallback=False)
    if out:
        return _ensure_idx(out, ensure_idx)

    # 2) relaxed (짧은 본문도 살림)
    out = _pass(40, allow_title_fallback=False)
    if out:
        return _ensure_idx(out, ensure_idx)

    # 3) last-resort (제목이라도 근거 카드가 0개로 보이는 상황 방지)
    out = _pass(15, allow_title_fallback=True)
    return _ensure_idx(out, ensure_idx)


def _ensure_idx(out: List[Dict[str, Any]], ensure_idx: bool) -> List[Dict[str, Any]]:
    if not ensure_idx:
        return out
    had_idx = any("idx" in x for x in out)
    if had_idx:
        for i, x in enumerate(out, start=1):
            x["idx"] = i
    return out
