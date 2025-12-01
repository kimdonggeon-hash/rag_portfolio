from __future__ import annotations

import os
import re
import json
import hashlib
import logging
import inspect
from typing import List, Dict, Tuple, Optional, Any
from datetime import datetime
from urllib.parse import (
    quote_plus,
    urlparse,
    parse_qs,
    urljoin,
)

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ⚠️ 순환 참조/의도치 오버라이드 방지를 위해 제거
# from ragapp.news_views.news_services import *

import requests
from django.conf import settings

try:
    # DB 기반 FAQ 후보 가져오는 함수
    from ragapp.qa_data import get_faq_candidates  # type: ignore
except Exception:
    # qa_data 마이그레이드 전이거나 에러 나도 전체 서비스가 죽지 않게
    get_faq_candidates = None  # type: ignore


log = logging.getLogger(__name__)

# =============================================================================
# 0) Google GenAI (Vertex 백엔드 고정): 텍스트 생성 / 임베딩
# =============================================================================

_GENAI_CLIENT = None


def _check_adc_env(project: Optional[str], location: Optional[str]) -> None:
    """ADC(서비스 계정) 환경 사전 점검: 프로젝트/리전/GAC 파일 경로 확인."""
    gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or getattr(
        settings, "GOOGLE_APPLICATION_CREDENTIALS", ""
    )
    gac = str(gac).strip()
    if not project:
        log.warning("Vertex 프로젝트 ID가 비어있습니다. (.env의 VERTEX_PROJECT_ID 또는 GOOGLE_CLOUD_PROJECT)")
    if not location:
        log.warning("Vertex 리전이 비어있습니다. (.env의 VERTEX_LOCATION 또는 GCP_LOCATION)")
    if gac:
        # 경로만 로그로 확인(없다면 이전에 겪은 File not found를 사전 경고)
        if not Path(gac).exists():
            log.warning("GOOGLE_APPLICATION_CREDENTIALS 경로가 존재하지 않습니다: %s", gac)
    else:
        log.debug("GOOGLE_APPLICATION_CREDENTIALS 미설정: gcloud/OS 로그인 등 다른 ADC 경로를 사용합니다.")


def _genai_client():
    """
    Vertex AI 백엔드로 고정된 google-genai Client 생성(1회 캐시).
    """
    global _GENAI_CLIENT
    if _GENAI_CLIENT is not None:
        return _GENAI_CLIENT

    try:
        from google import genai
        from google.genai import types as genai_types  # 일부 배포판 호환
    except Exception as e:
        raise RuntimeError(
            "google-genai 패키지가 없습니다. `pip install -U google-genai` 후 다시 시도하세요."
        ) from e

    project = getattr(settings, "VERTEX_PROJECT_ID", None) or os.environ.get("VERTEX_PROJECT_ID")
    location = getattr(settings, "VERTEX_LOCATION", None) or os.environ.get("VERTEX_LOCATION") or "us-central1"

    _check_adc_env(project, location)

    if not project:
        raise RuntimeError("VERTEX_PROJECT_ID 가 설정되어야 합니다. (settings.py 또는 환경변수)")

    # Vertex 라우팅 + stable v1 (가능한 한 보수적으로)
    try:
        http_opts = genai_types.HttpOptions(api_version="v1")
    except Exception:
        from google import genai as _g
        http_opts = _g.types.HttpOptions(api_version="v1")

    _GENAI_CLIENT = genai.Client(
        vertexai=True,
        project=project,
        location=location,
        http_options=http_opts,
    )
    return _GENAI_CLIENT


# ── 모델 선택 유틸 (★ 모델은 .env에서만 읽기 — settings/인자 무시)
def _first_non_empty(values: list[str | None]) -> str | None:
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _choose_text_model(_explicit_ignored: Optional[str]) -> str:
    """
    텍스트 모델은 무조건 .env에서만 선택.
    우선순위: GEMINI_MODEL_DIRECT > VERTEX_TEXT_MODEL > GEMINI_TEXT_MODEL > GEMINI_MODEL > GEMINI_MODEL_DEFAULT
    """
    picked = _first_non_empty(
        [
            os.environ.get("GEMINI_MODEL_DIRECT"),
            os.environ.get("VERTEX_TEXT_MODEL"),
            os.environ.get("GEMINI_TEXT_MODEL"),
            os.environ.get("GEMINI_MODEL"),
            os.environ.get("GEMINI_MODEL_DEFAULT"),
        ]
    )
    if picked:
        return picked
    raise RuntimeError(
        "텍스트 생성 모델명이 .env에 없습니다. "
        "GEMINI_MODEL_DIRECT 또는 VERTEX_TEXT_MODEL/GEMINI_TEXT_MODEL/GEMINI_MODEL 중 하나를 설정하세요."
    )


def _env_embed_model() -> str:
    """
    임베딩 모델은 무조건 .env에서만 선택.
    우선순위: VERTEX_EMBED_MODEL > GEMINI_EMBED_MODELS(첫 항목)
    """
    v = os.environ.get("VERTEX_EMBED_MODEL")
    if v and v.strip():
        return v.strip()
    m = os.environ.get("GEMINI_EMBED_MODELS")
    if m and m.strip():
        return m.split(",")[0].strip()
    raise RuntimeError(
        "임베딩 모델명이 .env에 없습니다. VERTEX_EMBED_MODEL 또는 GEMINI_EMBED_MODELS 를 설정하세요."
    )


# ★ 변경 포인트 1: 항상 비어있지 않은 답변을 보장하는 보수적 파서 + 폴백 문구
_EMPTY_FALLBACK = "API가 답변을 반환하지 않았습니다."


def _extract_text_from_genai_response(resp) -> str:
    """
    google-genai 응답에서 텍스트를 최대한 호환성 있게 추출.
    절대 예외 던지지 않음. 없으면 빈 문자열.
    """
    # 1) 단일 텍스트 속성
    try:
        t = getattr(resp, "text", None) or getattr(resp, "output_text", None)
        if isinstance(t, str) and t.strip():
            return t.strip()
    except Exception:
        pass

    # 2) candidates → content.parts[*].text
    try:
        cands = getattr(resp, "candidates", None) or []
        for c in cands:
            content = getattr(c, "content", None)
            if not content:
                continue
            parts = getattr(content, "parts", None) or []
            texts = []
            for p in parts:
                txt = getattr(p, "text", None)
                if isinstance(txt, str) and txt.strip():
                    texts.append(txt.strip())
                elif isinstance(p, str) and p.strip():
                    texts.append(p.strip())
            if texts:
                return "\n".join(texts).strip()
    except Exception:
        pass

    # 3) dict 계열 안전 파싱
    try:
        if isinstance(resp, dict):
            if isinstance(resp.get("text"), str) and resp["text"].strip():
                return resp["text"].strip()
            for c in resp.get("candidates") or []:
                parts = (((c or {}).get("content") or {}).get("parts") or [])
                texts = []
                for p in parts:
                    if isinstance(p, dict) and isinstance(p.get("text"), str) and p["text"].strip():
                        texts.append(p["text"].strip())
                    elif isinstance(p, str) and p.strip():
                        texts.append(p.strip())
                if texts:
                    return "\n".join(texts).strip()
    except Exception:
        pass

    return ""


def ask_gemini(prompt: str, model: Optional[str] = None) -> str:
    """
    Vertex 경유 텍스트 생성 (google-genai).
    - 절대 빈 문자열을 반환하지 않음
    - 대괄호([])로 시작하지 않음(템플릿의 '응답 없음' 오인 방지)
    - 실패/차단/빈응답 시 사람 읽을 수 있는 문장으로 돌려줌
    """
    client = _genai_client()
    mdl_name = _choose_text_model(model)

    try:
        # 1) 최신 포맷 시도: role/parts
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        resp = client.models.generate_content(model=mdl_name, contents=contents)
        txt = _extract_text_from_genai_response(resp)
        if txt and txt.strip():
            return txt.strip()

        # 2) 호환 포맷 폴백: 단일 문자열
        resp2 = client.models.generate_content(model=mdl_name, contents=prompt)
        txt2 = _extract_text_from_genai_response(resp2)
        if txt2 and txt2.strip():
            return txt2.strip()

        # 여기까지도 비면 차단/안전필터/포맷 이슈 가능 → finish_reason 힌트 뽑아보기
        finish = ""
        try:
            cands = getattr(resp2, "candidates", None) or getattr(resp, "candidates", None) or []
            if cands:
                fr = getattr(cands[0], "finish_reason", None) or getattr(
                    cands[0], "safety_ratings", None
                )
                finish = f" (finish_reason: {fr})"
        except Exception:
            pass

        log.warning("ask_gemini: empty text (model=%s)%s", mdl_name, finish)
        return f"모델이 텍스트 본문을 반환하지 않았습니다.{finish} 로그를 확인해 주세요."

    except Exception as e:
        msg = f"모델 호출 실패: {e}"
        log.warning("ask_gemini 실패(model=%s): %s", mdl_name, e)
        return msg


def _embed_texts(texts: List[str]) -> List[List[float]]:
    """
    텍스트 리스트 → 임베딩 리스트.
    - Vertex SDK(TextEmbeddingModel)만 사용
    - google-genai(API Key) 폴백은 제거 (서비스 계정 JSON/ADC 전용 모드)
    """
    if not texts:
        return []

    # 입력 정리
    batch = []
    for t in texts:
        s = ("" if t is None else str(t)).strip()
        batch.append(s if s else " ")

    # ── Vertex 만 사용 ──────────────────────────────────────────────
    try:
        project = (
            getattr(settings, "VERTEX_PROJECT_ID", None)
            or os.environ.get("VERTEX_PROJECT_ID")
            or getattr(settings, "VERTEX_PROJECT", None)
            or os.environ.get("VERTEX_PROJECT")
        )
        location = getattr(settings, "VERTEX_LOCATION", None) or os.environ.get("VERTEX_LOCATION") or "us-central1"
        if not project:
            raise RuntimeError("VERTEX_PROJECT_ID/PROJECT 미설정")

        import vertexai

        try:
            vertexai.init(project=project, location=location)
        except Exception:
            # 이미 init 되어 있거나, 경고 수준의 에러는 그냥 통과
            pass

        try:
            # 최신(프리뷰) API 우선
            from vertexai.preview.language_models import TextEmbeddingModel  # type: ignore
        except Exception:
            # 구버전 SDK fallback
            from vertexai.language_models import TextEmbeddingModel  # type: ignore

        model_name = _env_embed_model()
        model = TextEmbeddingModel.from_pretrained(model_name)

        # SDK 버전에 따른 인자 이름 차이 보정
        try:
            emb_objs = model.get_embeddings(batch)
        except TypeError:
            emb_objs = model.get_embeddings(input=batch)

        def _vec_from(obj) -> List[float]:
            # 다양한 응답 포맷을 최대한 호환성 있게 처리
            if hasattr(obj, "values"):
                return [float(x) for x in list(getattr(obj, "values"))]
            if hasattr(obj, "embedding"):
                emb = getattr(obj, "embedding")
                if hasattr(emb, "values"):
                    return [float(x) for x in list(getattr(emb, "values"))]
                if isinstance(emb, (list, tuple)):
                    return [float(x) for x in emb]
            if isinstance(obj, dict):
                if "values" in obj and isinstance(obj["values"], (list, tuple)):
                    return [float(x) for x in obj["values"]]
                emb = obj.get("embedding")
                if isinstance(emb, dict) and "values" in emb:
                    return [float(x) for x in emb["values"]]
                if isinstance(emb, (list, tuple)):
                    return [float(x) for x in emb]
            # 최후의 수단: iter 가능한 객체라고 가정
            return [float(x) for x in list(obj)]

        if isinstance(emb_objs, list):
            out = [_vec_from(e) for e in emb_objs]
        else:
            cand = getattr(emb_objs, "embeddings", None)
            out = [_vec_from(e) for e in (cand or [emb_objs])]

        if not out or any(not v for v in out):
            raise RuntimeError("Vertex 임베딩 응답 파싱 실패")

        return out

    except Exception as e_vertex:
        log.warning("Vertex 임베딩 실패: %s", e_vertex)
        # 🔴 더 이상 google-genai(API Key) 폴백은 사용하지 않고 여기서 바로 실패 처리
        raise RuntimeError(f"임베딩 실패(Vertex): {e_vertex}") from e_vertex



# =============================================================================
# ❶ 로컬 SQLite 벡터 스토어 (Chroma 대체)
# =============================================================================
import sqlite3
from math import sqrt


def _normalize_vector_path(raw: str | os.PathLike | None) -> str:
    """
    VECTOR_DB_PATH 경로를 안전하게 정규화한다.
    - 설정값/환경변수 없으면 BASE_DIR/vector_store.sqlite3 사용
    - Windows에서 잘못된 이스케이프(예: 'C:\\vscode'를 코드 리터럴로 잘못 쓴 경우)로
      제어문자가 들어간 경우 기본값으로 폴백
    """
    base = Path(getattr(settings, "BASE_DIR", Path.cwd()))
    if not raw:
        return str(base / "vector_store.sqlite3")

    s = str(raw)

    # 제어문자(탭, 줄바꿈, vertical tab 등)가 섞여 있으면 안전하게 기본 경로로 교체
    if any(ord(ch) < 32 for ch in s):
        log.warning("VECTOR_DB_PATH에 제어문자가 포함되어 기본 경로로 되돌립니다: %r", s)
        return str(base / "vector_store.sqlite3")

    try:
        p = Path(os.path.expanduser(os.path.expandvars(s)))
        return str(p.resolve())
    except Exception as e:
        log.warning("VECTOR_DB_PATH 정규화 실패(%s) → 기본 경로 사용: %r", e, s)
        return str(base / "vector_store.sqlite3")


# 로컬 벡터 DB 파일 경로 (환경변수 VECTOR_DB_PATH/설정값 VECTOR_DB_PATH로 변경 가능)
_VECTOR_DB_PATH = _normalize_vector_path(
    os.environ.get("VECTOR_DB_PATH") or getattr(settings, "VECTOR_DB_PATH", None)
)


def _sqlite_conn():
    p = Path(_VECTOR_DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vector_docs (
            id TEXT PRIMARY KEY,
            doc TEXT NOT NULL,
            meta_json TEXT NOT NULL,
            emb_json  TEXT NOT NULL
        )
    """
    )
    return conn


def _cosine_dist(a: list[float], b: list[float]) -> float:
    # 거리값은 "작을수록 가까움"이 되도록 1 - cosine_similarity
    if not a or not b or len(a) != len(b):
        return 1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sqrt(sum(x * x for x in a))
    nb = sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 1.0
    sim = dot / (na * nb)
    return 1.0 - float(sim)


def _sqlite_upsert(ids, docs, metas, embs):
    import json as _json

    with _sqlite_conn() as c:
        c.executemany(
            "REPLACE INTO vector_docs (id, doc, meta_json, emb_json) VALUES (?, ?, ?, ?)",
            [
                (i, d, _json.dumps(m, ensure_ascii=False), _json.dumps(e))
                for i, d, m, e in zip(ids, docs, metas, embs)
            ],
        )


def _sqlite_query_by_embedding(q_emb: list[float], topk: int, where: dict | None):
    import json as _json

    with _sqlite_conn() as c:
        rows = list(c.execute("SELECT id, doc, meta_json, emb_json FROM vector_docs"))
    docs, metas, dists, ids = [], [], [], []
    for rid, doc, mjson, ejson in rows:
        try:
            meta = _json.loads(mjson or "{}")
            emb = _json.loads(ejson or "[]")
        except Exception:
            continue
        # where 필터(간단: source / source_name)
        if where:
            src = (meta.get("source") or meta.get("source_name") or "").strip()
            ok = True
            if isinstance(where, dict) and "source" in where:
                cond = where["source"]
                if isinstance(cond, dict) and "$in" in cond:
                    ok = src in [str(x) for x in cond["$in"]]
                else:
                    ok = src == str(cond)
            if not ok:
                continue
        dist = _cosine_dist(q_emb, emb)
        docs.append(doc)
        metas.append(meta)
        dists.append(dist)
        ids.append(rid)
    order = sorted(range(len(dists)), key=lambda i: dists[i])[: max(1, int(topk))]
    return {
        "documents": [[docs[i] for i in order]],
        "metadatas": [[metas[i] for i in order]],
        "distances": [[dists[i] for i in order]],
        "ids": [[ids[i] for i in order]],
    }


# (호환용) 원래 chroma의 collection 객체를 반환하던 함수 자리에 noop 제공
def _chroma_collection():
    return None


# =============================================================================
# 1) 뉴스 RSS / 본문 크롤링
# =============================================================================

UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124 Safari/537.36"
)
UA_MOBILE = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124 Mobile Safari/537.36"
)
ACCEPT_LANG = "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"
ACCEPT_GENERIC = (
    "text/html,application/xhtml+xml,application/xml;"
    "q=0.9,image/avif,image/webp,*/*;q=0.8"
)


def _http_get_with_ua(url: str, timeout: int) -> Optional[str]:
    headers_list = [
        {
            "User-Agent": UA_DESKTOP,
            "Accept-Language": ACCEPT_LANG,
            "Accept": ACCEPT_GENERIC,
            "Referer": "https://www.google.com/",
        },
        {
            "User-Agent": UA_MOBILE,
            "Accept-Language": ACCEPT_LANG,
            "Accept": ACCEPT_GENERIC,
            "Referer": "https://www.google.com/",
        },
    ]
    for hdr in headers_list:
        try:
            r = requests.get(url, headers=hdr, timeout=timeout, allow_redirects=True)
            if getattr(r, "ok", False):
                return r.text
        except Exception as e:
            log.debug("_http_get_with_ua fail(%s): %s", url, e)
    return None


def _google_news_unwrap(url: str) -> str:
    try:
        parsed = urlparse(url)
        if "news.google." in parsed.netloc:
            qs = parse_qs(parsed.query)
            if "url" in qs and qs["url"]:
                return qs["url"][0]
    except Exception:
        pass
    return url


def _resolve_redirect(url: str, timeout: int = 12) -> Tuple[str, Optional[str]]:
    try:
        first = _google_news_unwrap(url)
        if first != url:
            return first, None

        heads = {
            "User-Agent": UA_DESKTOP,
            "Accept-Language": ACCEPT_LANG,
            "Accept": ACCEPT_GENERIC,
            "Referer": "https://www.google.com/",
        }
        try:
            r_head = requests.head(url, headers=heads, timeout=timeout, allow_redirects=True)
            if getattr(r_head, "ok", False) and getattr(r_head, "url", None):
                return r_head.url, None
        except Exception:
            pass

        try:
            r_get = requests.get(url, headers=heads, timeout=timeout, allow_redirects=True)
            final_url = getattr(r_get, "url", None) or url
            html_txt = getattr(r_get, "text", None)
            if getattr(r_get, "ok", False):
                return final_url, html_txt
            return final_url, None
        except Exception:
            return url, None
    except Exception:
        return url, None


def _detect_client_redirect(html_text: str, base_url: str) -> Optional[str]:
    if not html_text:
        return None

    m = re.search(
        r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\'][^"\']*url=([^"\']+)["\']',
        html_text,
        flags=re.IGNORECASE,
    )
    if m:
        return urljoin(base_url, m.group(1).strip())

    m2 = re.search(
        r'location\.(?:replace|href)\s*=\s*["\']([^"\']+)["\']',
        html_text,
        flags=re.IGNORECASE,
    )
    if m2:
        return urljoin(base_url, m2.group(1).strip())

    return None


def _follow_client_redirects(
    start_url: str,
    first_html: Optional[str],
    timeout: int = 12,
    max_hops: int = 3,
) -> Tuple[str, Optional[str]]:
    cur_url = start_url
    cur_html = first_html

    for _ in range(max_hops):
        if not cur_html:
            cur_html = _http_get_with_ua(cur_url, timeout=timeout)
            if not cur_html:
                break

        nxt = _detect_client_redirect(cur_html, cur_url)
        if not nxt:
            break

        cur_url = nxt
        cur_html = _http_get_with_ua(cur_url, timeout=timeout)

    return cur_url, cur_html


def _text_len_score(html_candidate: Optional[str]) -> int:
    if not html_candidate:
        return 0
    txt_only = re.sub(r"<[^>]+>", " ", html_candidate)
    txt_only = re.sub(r"\s+", " ", txt_only).strip()
    return len(txt_only)


def _guess_amp_candidates(url: str) -> List[str]:
    cands: List[str] = []
    if not re.search(r"/amp(/)?$", url):
        if url.endswith("/"):
            cands.append(url + "amp")
            cands.append(url + "amp/")
        else:
            cands.append(url.rstrip("/") + "/amp")
            cands.append(url.rstrip("/") + "/amp/")
    if "output=amp" not in url:
        if "?" in url:
            cands.append(url + "&output=amp")
        else:
            cands.append(url + "?output=amp")
    return cands


def _render_with_headless(final_url: str, timeout: int):
    use_headless = getattr(settings, "USE_HEADLESS_BROWSER", False)
    if not use_headless:
        return None
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        log.warning("Playwright import 실패: %s", e)
        return None

    try:
        headless_timeout = int(getattr(settings, "HEADLESS_TIMEOUT_SEC", timeout))
    except Exception:
        headless_timeout = timeout

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=UA_DESKTOP,
                locale="ko-KR",
                extra_http_headers={
                    "Accept-Language": ACCEPT_LANG,
                    "Accept": ACCEPT_GENERIC,
                    "Referer": "https://www.google.com/",
                },
            )
            page = context.new_page()
            page.goto(final_url, timeout=headless_timeout * 1000)
            try:
                page.wait_for_load_state("networkidle", timeout=headless_timeout * 1000)
            except Exception:
                pass
            for _ in range(3):
                page.mouse.wheel(0, 2000)
                page.wait_for_timeout(800)
            html_after_js = page.content()
            browser.close()
            return html_after_js
    except Exception as e:
        log.warning("Playwright 렌더 실패 %s : %s", final_url, e)
        return None


def _fetch_html_full(
    final_url: str,
    first_html: Optional[str],
    timeout: int = 12,
):
    plain_html = first_html
    rendered_html = None
    amp_html = None

    if not plain_html:
        plain_html = _http_get_with_ua(final_url, timeout=timeout)

    need_render = _text_len_score(plain_html) < 400
    if need_render:
        rendered_html = _render_with_headless(final_url, timeout=timeout)

    best_len_so_far = max(_text_len_score(plain_html), _text_len_score(rendered_html))
    if best_len_so_far < 400:
        for amp_url in _guess_amp_candidates(final_url):
            amp_try = _http_get_with_ua(amp_url, timeout=timeout)
            if _text_len_score(amp_try) > best_len_so_far:
                amp_html = amp_try
                best_len_so_far = _text_len_score(amp_try)
            if best_len_so_far >= 400:
                break

    return plain_html, rendered_html, amp_html


def _extract_site_specific(final_url: str, html_text: str) -> str:
    if not final_url or not html_text:
        return ""
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return ""

    soup = BeautifulSoup(html_text, "html.parser")
    host = ""
    try:
        host = urlparse(final_url).netloc.lower()
    except Exception:
        pass

    # NAVER
    if "naver.com" in host:
        el = soup.find(id="dic_area")
        if el:
            txt = el.get_text(separator="\n", strip=True)
            if txt and len(txt) > 50:
                return txt.strip()

        for cand_id in ["newsct_article", "newsct_body", "articleBodyContents"]:
            el2 = soup.find(id=cand_id)
            if el2:
                txt2 = el2.get_text(separator="\n", strip=True)
                if txt2 and len(txt2) > 50:
                    return txt2.strip()

        common_classes = ["go_trans _article_content", "newsct_article", "article_body"]
        for cls in common_classes:
            for div in soup.find_all("div", class_=cls):
                txt3 = div.get_text(separator="\n", strip=True)
                if txt3 and len(txt3) > 50:
                    return txt3.strip()

    # DAUM
    if "daum.net" in host:
        for div in soup.find_all("div", class_=re.compile(r"article_view|viewer")):
            txt = div.get_text(separator="\n", strip=True)
            if txt and len(txt) > 50:
                return txt.strip()

        sections = soup.find_all("section")
        sec_texts: List[str] = []
        for sec in sections:
            t = sec.get_text(separator="\n", strip=True)
            if t and len(t) > 50:
                sec_texts.append(t)
        if sec_texts:
            return max(sec_texts, key=len).strip()

    # CHOSUN
    if "chosun.com" in host:
        target = soup.find(id=re.compile(r"news_body|art_text|article", re.I))
        if target:
            t = target.get_text(separator="\n", strip=True)
            if t and len(t) > 50:
                return t.strip()

    # HANI
    if "hani.co.kr" in host:
        target = soup.find("div", class_=re.compile(r"article-text|article-body", re.I))
        if target:
            t = target.get_text(separator="\n", strip=True)
            if t and len(t) > 50:
                return t.strip()

    return ""


def _extract_trafilatura(html_text: str) -> str:
    try:
        import trafilatura

        txt = trafilatura.extract(
            html_text,
            output_format="txt",
            include_links=False,
            include_comments=False,
            favor_recall=True,
            no_fallback=False,
        )
        return (txt or "").strip()
    except Exception:
        return ""


def _extract_readability(html_text: str) -> str:
    try:
        from readability import Document
        from lxml import html as lhtml

        frag_html = Document(html_text).summary(html_partial=True)
        only_text = lhtml.fromstring(frag_html).text_content()
        return (only_text or "").strip()
    except Exception:
        return ""


def _extract_newspaper3k(final_url: str, html_text: Optional[str]) -> str:
    try:
        from newspaper import Article

        art = Article(final_url, language="ko")
        if html_text:
            art.download(input_html=html_text)
        else:
            art.download()
        art.parse()
        return (art.text or "").strip()
    except Exception:
        return ""


def _extract_boilerpy3(html_text: str) -> str:
    try:
        from boilerpy3 import extractors

        extr = extractors.ArticleExtractor()
        txt = extr.get_content(html_text or "")
        return (txt or "").strip()
    except Exception:
        return ""


def _extract_bs4_maintext(html_text: str) -> str:
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return ""

    if not html_text:
        return ""

    soup = BeautifulSoup(html_text, "html.parser")
    cands: List[str] = []

    for art in soup.find_all("article"):
        t = art.get_text(separator="\n", strip=True)
        if t and len(t) > 50:
            cands.append(t)

    KEY_HINTS = (
        "article",
        "art_body",
        "news",
        "content",
        "article-body",
        "news_body",
        "read_body",
        "detail_body",
        "story",
        "post-body",
        "articleBody",
        "viewer",
        "view",
        "dic_area",
        "article_view",
    )
    for tag in soup.find_all(["div", "section", "span"]):
        tid = tag.get("id") or ""
        cls_list = tag.get("class") or []
        hay = " ".join([tid] + cls_list)
        if any(hint.lower() in hay.lower() for hint in KEY_HINTS):
            t = tag.get_text(separator="\n", strip=True)
            if t and len(t) > 50:
                cands.append(t)

    for sc in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            raw = (sc.string or sc.text or "").strip()
            if not raw:
                continue
            data = json.loads(raw)
            objs = data if isinstance(data, list) else [data]
            for obj in objs:
                if isinstance(obj, dict):
                    body_txt = obj.get("articleBody") or obj.get("description") or ""
                    if body_txt and len(body_txt.strip()) > 50:
                        cands.append(body_txt.strip())
        except Exception:
            pass

    if not cands:
        return ""

    best = max(cands, key=len)
    return best.strip()


# ★ 구글뉴스 중계 페이지에서 원문 URL 추출
def _extract_google_news_original_url(html_text: str, base_url: str) -> Optional[str]:
    if not html_text:
        return None
    try:
        from bs4 import BeautifulSoup
    except Exception:
        BeautifulSoup = None

    def _is_external(u: str) -> bool:
        try:
            host = urlparse(u).netloc.lower()
        except Exception:
            return False
        if not host:
            return False
        if "news.google." in host or host.endswith("google.com"):
            return False
        return True

    if BeautifulSoup is not None:
        soup = BeautifulSoup(html_text, "html.parser")
        tag = soup.find("meta", attrs={"property": "og:url"})
        cand = tag.get("content").strip() if tag and tag.get("content") else ""
        if cand and _is_external(cand):
            return cand
        link_tag = soup.find("link", attrs={"rel": ["canonical", "Canonical", "CANONICAL"]})
        if link_tag and link_tag.get("href"):
            cand2 = link_tag["href"].strip()
            if cand2 and _is_external(cand2):
                return cand2
        for a in soup.find_all("a", href=True):
            cand3 = a["href"].strip()
            if cand3.startswith("http") and _is_external(cand3):
                return cand3

    for m in re.findall(r"https://[^\s\"\'<>]+", html_text):
        if _is_external(m):
            return m
    return None


def fetch_article_text(url: str, timeout: int = 12) -> str:
    try:
        final_url, pre_html = _resolve_redirect(url, timeout=timeout)
        final_url2, pre_html2 = _follow_client_redirects(
            final_url, pre_html, timeout=timeout, max_hops=3
        )

        # 구글뉴스 중계면 원문 복원 시도
        try:
            host2 = urlparse(final_url2).netloc.lower()
        except Exception:
            host2 = ""
        if "news.google." in host2:
            html_for_extract = pre_html2 or _http_get_with_ua(final_url2, timeout=timeout)
            real_u = _extract_google_news_original_url(html_for_extract or "", final_url2)
            if real_u:
                return fetch_article_text(real_u, timeout=timeout)

        plain_html, rendered_html, amp_html = _fetch_html_full(final_url2, pre_html2, timeout=timeout)

        # 사이트별 룰 먼저
        for html_src in (plain_html, rendered_html, amp_html):
            if not html_src:
                continue
            site_txt = _extract_site_specific(final_url2, html_src)
            if site_txt and len(site_txt.strip()) > 50:
                return site_txt.strip()

        # 일반 추출기들
        cands: List[str] = []

        def _try_all(src_name: str, html_src: Optional[str]):
            if not html_src:
                return
            t1 = _extract_trafilatura(html_src)
            t2 = _extract_readability(html_src)
            t3 = _extract_newspaper3k(final_url2, html_src)
            t4 = _extract_boilerpy3(html_src)
            t5 = _extract_bs4_maintext(html_src)
            for tx in (t1, t2, t3, t4, t5):
                if tx and tx.strip():
                    cands.append(tx.strip())

        _try_all("plain_html", plain_html)
        _try_all("rendered_html", rendered_html)
        _try_all("amp_html", amp_html)

        if not cands:
            return ""
        return max(cands, key=len).strip()

    except Exception as e:
        log.warning("fetch_article_text 예외 url=%s err=%s", url, e)
        return ""


def _clean_text_for_preview(raw_text: str, fallback_snippet: str = "") -> str:
    if not raw_text:
        return (fallback_snippet or "").strip()[:500]

    allowed_chars = []
    for ch in raw_text:
        code = ord(ch)
        is_basic_ws = ch in ("\n", "\r", "\t", " ")
        is_basic_ascii = 32 <= code <= 126
        is_hangul = 0x1100 <= code <= 0x11FF or 0x3130 <= code <= 0x318F or 0xAC00 <= code <= 0xD7A3
        is_cjk = 0x4E00 <= code <= 0x9FFF
        is_fullwidth = 0xFF00 <= code <= 0xFFEF
        is_cjk_punct = 0x3000 <= code <= 0x303F
        if is_basic_ws or is_basic_ascii or is_hangul or is_cjk or is_fullwidth or is_cjk_punct:
            allowed_chars.append(ch)

    cleaned = "".join(allowed_chars)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n\s*\n\s*\n+", "\n\n", cleaned).strip()

    if len(cleaned) < 30 and fallback_snippet:
        cleaned = fallback_snippet.strip()

    return cleaned[:500]


def crawl_news_bodies(news_list: List[Dict[str, str]], max_workers: int = 6) -> List[Dict[str, str]]:
    out = [dict(n) for n in (news_list or [])]
    if not out:
        return out

    def job(n: Dict[str, str]) -> Dict[str, str]:
        u = (n.get("url") or "").strip()
        raw_body = fetch_article_text(u, timeout=12)
        n["news_body"] = raw_body or ""
        n["news_preview"] = _clean_text_for_preview(
            raw_body or "", fallback_snippet=n.get("snippet", "")
        )
        n["body_len"] = len(raw_body or "")
        return n

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(job, n): i for i, n in enumerate(out)}
        for f in as_completed(futs):
            i = futs[f]
            try:
                out[i] = f.result()
            except Exception as e:
                log.warning("crawl_news_bodies 작업 실패: %s", e)
                out[i]["news_body"] = ""
                out[i]["news_preview"] = ""
                out[i]["body_len"] = 0

    return out


def search_news_rss(query: str, top_k: int) -> List[Dict[str, str]]:
    tmpl = getattr(
        settings,
        "NEWS_RSS_QUERY_TEMPLATE",
        os.environ.get(
            "NEWS_RSS_QUERY_TEMPLATE",
            "https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko",
        ),
    )
    url = tmpl.format(query=quote_plus(query))

    try:
        import feedparser
    except Exception as e:
        log.warning("feedparser 미설치 또는 오류: %s", e)
        return []

    feed = feedparser.parse(url)
    arts: List[Dict[str, str]] = []
    for entry in feed.get("entries", [])[:top_k]:
        link = entry.get("link", "")
        src = ""
        try:
            src = (entry.get("source") or {}).get("title", "")
        except Exception:
            src = ""
        if not src:
            try:
                src = urlparse(link).netloc
            except Exception:
                src = ""

        arts.append(
            {
                "title": entry.get("title", "") or "",
                "url": link,
                "source": src,
                "published_at": entry.get("published") or entry.get("updated") or "",
                "snippet": entry.get("summary", "") or "",
            }
        )
    return arts


# ★ 변경 포인트 2: 항상 비어있지 않은 답변을 반환하도록 보강
def gemini_answer_with_news(question: str) -> Tuple[str, List[Dict[str, str]]]:
    """
    1) Vertex로 answer_text 생성
    2) 관련 최신 뉴스 헤드라인 목록 반환
    """
    prompt = (
        "한국어로 간결하고 최신성을 반영해서 답하세요.\n"
        "가능하면 참고할 만한 기사나 자료 URL을 본문 하단에 목록 형태로 3~5개 적어 주세요.\n\n"
        f"[질문]\n{question}\n\n[답변]\n"
    )
    answer_text = ask_gemini(prompt, model=None)

    try:
        topk = int(getattr(settings, "NEWS_TOPK", os.environ.get("NEWS_TOPK", "5")))
    except Exception:
        topk = 5

    headlines = search_news_rss(question, topk)

    # 최종 보정: 빈 문자열이나 공백/형식문자만 오면 폴백 문구로 대체
    if not isinstance(answer_text, str) or not answer_text.strip():
        answer_text = _EMPTY_FALLBACK

    return answer_text.strip(), headlines


# =============================================================================
# 2) 인덱싱 (현재: 로컬 SQLite 벡터 스토어 사용)
# =============================================================================


def _sha(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8", "ignore")).hexdigest()[:16]


def _slug(s: str, n: int = 60) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z가-힣\-_. ]+", "", s or "")
    cleaned = re.sub(r"\s+", "-", cleaned).strip("-")
    return cleaned[:n] or "doc"


def _iso(dt) -> str:
    from email.utils import parsedate_to_datetime

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


def _chunk_text(text: str, size=1600, overlap=200) -> List[str]:
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


def _current_embed_dim() -> int:
    try:
        vec = _embed_texts(["__dim_probe__"])[0]
        return len(vec)
    except Exception:
        return -1


_URL_RAW = re.compile(r"(https?://[^\s<>\]\)\"']+)")
_URL_MD = re.compile(r"\[[^\]]+\]\((https?://[^\s)]+)\)")


def _extract_urls_from_answer(text: str, max_n: int = 6) -> List[str]:
    if not text:
        return []
    urls: List[str] = []
    try:
        urls += _URL_MD.findall(text)
    except Exception:
        pass
    try:
        urls += _URL_RAW.findall(text)
    except Exception:
        pass

    out: List[str] = []
    seen = set()
    for u in urls:
        u = u.strip().rstrip(").,]")
        if not u.lower().startswith(("http://", "https://")):
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= max_n:
            break
    return out


# --- (전역) 호환 upsert: Chroma 스타일 & 내부 명명 모두 허용 --------------------
def chroma_upsert(
    *args,
    **kwargs,
) -> Dict[str, Any]:
    """
    Compatibility upsert.
    지원 형태:
      1) 위치인자 4개: (ids, docs, metas, embs)
      2) 키워드:
         - ids=..., documents=..., metadatas=..., embeddings=...
         - ids=..., docs=..., metas=..., embs=...
    임베딩이 비어오면 자동으로 생성합니다.
    """
    # 1) 위치 인자
    if args and len(args) == 4:
        ids_a, docs_a, metas_a, embs_a = args
        ids = list(ids_a or [])
        documents = list(docs_a or [])
        metadatas = list(metas_a or [{} for _ in documents])
        embeddings = list(embs_a or [])
    else:
        # 2) 키워드 인자
        ids = list(kwargs.get("ids") or [])
        documents = list(kwargs.get("documents") or kwargs.get("docs") or [])
        metadatas = list(kwargs.get("metadatas") or kwargs.get("metas") or [{} for _ in documents])
        embeddings = list(kwargs.get("embeddings") or kwargs.get("embs") or [])

    # 길이 보정
    n = len(documents)
    if len(metadatas) != n:
        if len(metadatas) < n:
            metadatas.extend({} for _ in range(n - len(metadatas)))
        else:
            metadatas = metadatas[:n]

    # 임베딩이 없으면 생성
    if not embeddings or len(embeddings) != n:
        embeddings = _embed_texts(documents)

    # ID 기본값 생성 (비어있으면)
    if not ids or len(ids) != n:
        now_iso = datetime.utcnow().isoformat()
        ids = [f"doc:{_sha((documents[i] or '')[:80])}:{i}:{_sha(now_iso)}" for i in range(n)]

    _sqlite_upsert(ids, documents, metadatas, embeddings)
    return {"inserted": n, "note": "SQLite vector store upsert (compat shim)"}


def indexto_chroma_safe(
    question: str,
    answer: str,
    news_list: List[Dict[str, Any]],
) -> Dict[str, object]:
    """
    (서비스 버전) 답변/뉴스/답변내 링크를 안전하게 현재 벡터 DB에 저장.
    """
    size = int(
        getattr(settings, "EMBED_CHUNK_SIZE", os.environ.get("EMBED_CHUNK_SIZE", "1600"))
    )
    overlap = int(
        getattr(settings, "EMBED_CHUNK_OVERLAP", os.environ.get("EMBED_CHUNK_OVERLAP", "200"))
    )
    now_iso = datetime.utcnow().isoformat()

    ids: List[str] = []
    docs: List[str] = []
    metas: List[Dict[str, Any]] = []

    # A) 모델 answer → 청크
    if answer:
        ans_chunks = _chunk_text(answer, size=size, overlap=overlap)
        base_a = f"answer:{_sha(question)}"
        for i, ch in enumerate(ans_chunks):
            ch_s = (ch or "").strip()
            if not ch_s:
                continue
            ids.append(f"{base_a}:{i}")
            docs.append(ch_s)
            metas.append(
                {
                    "source": "web_answer",
                    "title": "웹검색 답변",
                    "question": question,
                    "ingested_at": now_iso,
                }
            )

    # B) 뉴스 → 메타 + 본문
    news_meta_only_count = 0
    for art in (news_list or []):
        url0 = (art.get("url") or "").strip()
        title0 = (art.get("title") or "").strip() or (
            urlparse(url0).netloc if url0 else "뉴스"
        )
        body = (art.get("news_body") or "").strip()
        body_len = len(body)

        base = f"news:{_slug(title0)}:{_sha(url0 or title0)}"

        meta_lines = [
            f"[META ONLY] {title0}",
            f"URL: {url0}" if url0 else "URL: (없음)",
            f"출처: {art.get('source', '')}",
            f"게시: {_iso(art.get('published_at'))}",
            (art.get("snippet") or "")[:300],
            f"(body_len={body_len})",
        ]
        meta_doc = "\n".join([ln for ln in meta_lines if ln]).strip()
        if meta_doc:
            ids.append(f"{base}:meta")
            docs.append(meta_doc)
            metas.append(
                {
                    "source": "news",
                    "meta_only": (not body),
                    "url": url0,
                    "title": title0,
                    "source_name": art.get("source", ""),
                    "published_at": art.get("published_at", ""),
                    "ingested_at": now_iso,
                    "body_len": body_len,
                }
            )
            if not body:
                news_meta_only_count += 1

        if body:
            body_chunks = _chunk_text(body, size=size, overlap=overlap)
            for idx, ch in enumerate(body_chunks):
                ch_s = (ch or "").strip()
                if not ch_s:
                    continue
                ids.append(f"{base}:{idx}")
                docs.append(ch_s)
                metas.append(
                    {
                        "source": "news",
                        "url": url0,
                        "title": title0,
                        "source_name": art.get("source", ""),
                        "published_at": art.get("published_at", ""),
                        "ingested_at": now_iso,
                        "body_len": body_len,
                    }
                )

    # C) 답변 내 링크들도 크롤링 (옵션)
    crawl_answer_links_flag = getattr(
        settings,
        "CRAWL_ANSWER_LINKS",
        os.environ.get("CRAWL_ANSWER_LINKS", "1") not in ("0", "false", "False"),
    )
    if crawl_answer_links_flag:
        max_links = int(
            getattr(settings, "ANSWER_LINK_MAX", os.environ.get("ANSWER_LINK_MAX", "5"))
        )
        timeout_s = int(
            getattr(
                settings,
                "ANSWER_LINK_TIMEOUT",
                os.environ.get("ANSWER_LINK_TIMEOUT", "12"),
            )
        )
        urls = _extract_urls_from_answer(answer, max_n=max_links)
        for u in urls:
            body2 = fetch_article_text(u, timeout=timeout_s)
            if not body2:
                continue
            body2_len = len(body2)
            link_chunks = _chunk_text(body2, size=size, overlap=overlap)
            base_l = f"anslink:{_slug(urlparse(u).netloc)}:{_sha(u)}"
            for idx, ch in enumerate(link_chunks):
                ch_s = (ch or "").strip()
                if not ch_s:
                    continue
                ids.append(f"{base_l}:{idx}")
                docs.append(ch_s)
                metas.append(
                    {
                        "source": "answer_link",
                        "url": u,
                        "question": question,
                        "ingested_at": now_iso,
                        "body_len": body2_len,
                    }
                )

    clean = [
        (idv, docv, metav)
        for (idv, docv, metav) in zip(ids, docs, metas)
        if docv and isinstance(docv, str) and docv.strip()
    ]

    if not clean:
        return {
            "status": "ok",
            "inserted": 0,
            "answer_chunks": 0,
            "news_total_chunks": 0,
            "news_meta_only_chunks": news_meta_only_count,
            "engine": "vector_db",
            "vector_db_path": _VECTOR_DB_PATH,
            "collection": None,
            "dir": None,
            "ingested_at": now_iso,
            "note": "인덱싱할 데이터가 없습니다.",
        }

    ids2, docs2, metas2 = map(list, zip(*clean))

    # 전역 chroma_upsert 사용 (임베딩 자동 생성/검증 포함)
    chroma_upsert(ids=ids2, documents=docs2, metadatas=metas2)

    ans_chunks = sum(1 for m in metas2 if m.get("source") == "web_answer")
    news_chunks = sum(1 for m in metas2 if m.get("source") == "news")

    return {
        "status": "ok",
        "inserted": len(ids2),
        "answer_chunks": ans_chunks,
        "news_total_chunks": news_chunks,
        "news_meta_only_chunks": news_meta_only_count,
        "engine": "vector_db",
        "vector_db_path": _VECTOR_DB_PATH,
        "collection": None,
        "dir": None,
        "ingested_at": now_iso,
    }


# =============================================================================
# 3) RAG 검색/답변 (현재: 로컬 SQLite 벡터 스토어 사용)
# =============================================================================


def _normalize_where_filter(v) -> Optional[Dict[str, Any]]:
    if v is None:
        return None
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                d = json.loads(s)
                return d if isinstance(d, dict) else None
            except Exception:
                pass
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


def _chroma_query_with_embeddings(
    col,  # 더 이상 사용하지 않지만 시그니처 호환을 위해 남김
    query: str,
    topk: int,
    where: Optional[Dict[str, Any]] = None,
    include: Optional[List[str]] = None,
):
    q_emb = _embed_texts([query])[0]
    where_fixed = _normalize_where_filter(where)
    return _sqlite_query_by_embedding(q_emb, topk, where_fixed)

def _attach_faq_hits(question: str, hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    기존 RAG hits 리스트에 FAQ 후보들을 '소스'로 추가.
    - 이미 붙어 있는 FAQ 항목은 중복 제거
    """
    if get_faq_candidates is None:
        return hits

    try:
        faq_cands = get_faq_candidates(question, top_k=3)
    except Exception as e:
        log.warning("get_faq_candidates 실패: %s", e)
        return hits

    if not faq_cands:
        return hits

    merged: List[Dict[str, Any]] = list(hits or [])
    seen = set()

    # 기존 hits 중 FAQ 소스는 중복 방지
    for h in merged:
        m = h.get("meta") or {}
        if (m.get("source") == "faq") or (m.get("source_name") == "faq"):
            key = ((m.get("title") or ""), (h.get("snippet") or ""))
            seen.add(key)

    # 새 FAQ 후보 붙이기
    for cand in faq_cands:
        title = f"[FAQ] {cand.get('q', '')}"
        snippet = cand.get("a", "") or ""
        key = (title, snippet)
        if key in seen:
            continue
        seen.add(key)

        merged.append(
            {
                "meta": {
                    "title": title,
                    "source_name": "faq",
                    "source": "faq",
                    "url": "",
                },
                "snippet": snippet,
                "score": cand.get("score"),
            }
        )

    return merged


def _maybe_override_with_faq_answer(question: str, answer_text: str) -> str:
    """
    질문이 FAQ와 거의 동일하면, 모델 답변 대신 FAQ 답변으로 갈아끼우기.
    - get_faq_candidates 점수 기준으로 판단
    """
    if get_faq_candidates is None:
        return answer_text

    try:
        faq_best_list = get_faq_candidates(question, top_k=1)
    except Exception:
        return answer_text

    if not faq_best_list:
        return answer_text

    best = faq_best_list[0]

    try:
        score = float(best.get("score", 0.0))
    except Exception:
        score = 0.0

    # 🔧 필요하면 0.75 / 0.8 / 0.9 등으로 조절해서 써도 됨
    if score >= 0.85 and best.get("a"):
        # 질문이 거의 그대로 FAQ인 경우 → FAQ 답변을 그대로 사용
        return best["a"]

    return answer_text

def _parse_hits_from_res(res):
    def _pick(v):
        return v[0] if (isinstance(v, list) and v and isinstance(v[0], list)) else (v or [])

    docs = _pick(res.get("documents"))
    metas = _pick(res.get("metadatas"))
    ids_ = _pick(res.get("ids")) if "ids" in res else [""] * len(docs)
    dists = _pick(res.get("distances"))

    hits = []
    for i, doc in enumerate(docs):
        if not doc:
            continue
        snippet = ((doc[:800] if isinstance(doc, str) else str(doc)).replace("\n", " ").strip())
        m = metas[i] if i < len(metas) else {}
        try:
            score = float(dists[i]) if (dists and i < len(dists) and dists[i] is not None) else None
        except Exception:
            score = None
        hits.append(
            {
                "id": ids_[i] if i < len(ids_) else "",
                "score": score,
                "meta": m,
                "snippet": snippet,
            }
        )
    return hits


def _rank_and_dedupe_hits(hits: List[Dict[str, Any]], max_n: int = 8) -> List[Dict[str, Any]]:
    def key_of(h):
        m = h.get("meta") or {}
        return (
            (m.get("url") or "").strip().lower(),
            (m.get("title") or "").strip(),
            (h.get("snippet") or "")[:120],
        )

    def score_of(h):
        s = h.get("score")
        try:
            return float(s) if s is not None else 1e9
        except Exception:
            return 1e9

    ordered = sorted(hits, key=score_of)  # 거리 낮을수록 가까움
    seen = set()
    out: List[Dict[str, Any]] = []
    for h in ordered:
        k = key_of(h)
        if k in seen:
            continue
        seen.add(k)
        out.append(h)
        if len(out) >= max_n:
            break
    return out


def _build_source_block(hits: List[Dict[str, Any]]) -> str:
    lines = []
    for i, h in enumerate(hits, start=1):
        m = h.get("meta") or {}
        title = (m.get("title") or m.get("url") or "문서").strip()
        source_name = (
            m.get("source_name") or m.get("source") or urlparse(m.get("url") or "").netloc or ""
        ).strip()
        snippet = (h.get("snippet") or "").strip()[:700]
        lines.append(f"[{i}] {title} · {source_name}\n{snippet}")
    return "\n\n".join(lines)


def _make_rag_prompt(question: str, source_block: str, hard: bool = False) -> str:
    if hard:
        return (
            "아래 '근거 자료'에 있는 내용만 사용해 한국어로 핵심을 정리해 답하세요.\n"
            "- 문장/항목 끝에 반드시 [1], [2]처럼 근거 번호 인용을 붙이세요.\n"
            "- 직접적 근거가 부족하면 '자료 내 직접 근거 부족' 한 줄만 쓰고 추측은 금지합니다.\n"
            "- 군더더기 없이 핵심만 요약하세요.\n\n"
            f"[질문]\n{question}\n\n[근거 자료]\n{source_block}\n\n=== 답변 시작 ===\n"
        )
    return (
        "아래 '근거 자료'를 최우선으로 참고해 한국어로 4~8문장으로 핵심을 답하세요.\n"
        "- 가능하면 문장 끝에 [1], [2]처럼 근거 번호를 붙이되, 직접 근거가 없으면 인용은 생략 가능합니다.\n"
        "- 근거가 부족한 부분은 일반 지식/상식으로 간결히 보완하세요(과도한 추측 금지).\n"
        "- 불필요한 서론 없이 핵심만.\n\n"
        f"[질문]\n{question}\n\n[근거 자료]\n{source_block}\n\n=== 답변 시작 ===\n"
    )


def rag_answer_grounded(
    question: str,
    initial_topk: int = 5,
    fallback_topk: int = 12,
    max_sources: int = 8,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    1) 로컬 벡터 스토어(SQLite/Chroma 대체)에서 근거 검색
    2) Gemini로 답변 생성
    3) FAQ 후보를 소스(hits)에 추가하고, 필요시 메인 답변을 FAQ로 교체
    """
    col = _chroma_collection()
    sources_filter = getattr(settings, "RAG_SOURCES_FILTER", None)

    rag_force_answer = getattr(
        settings,
        "RAG_FORCE_ANSWER",
        os.environ.get("RAG_FORCE_ANSWER", "1").lower() not in ("0", "false", "no"),
    )

    # ─ 1차 검색 ────────────────────────────────────────────
    res1 = _chroma_query_with_embeddings(col, question, initial_topk, where=sources_filter)
    hits1_all = _parse_hits_from_res(res1)
    hits1 = _rank_and_dedupe_hits(hits1_all, max_sources)
    block1 = _build_source_block(hits1)

    ans1 = ask_gemini(_make_rag_prompt(question, block1, hard=not rag_force_answer), model=None)

    def _weak(a: str) -> bool:
        t = (a or "").strip()
        return (not t) or (len(t) < 120) or (t == _EMPTY_FALLBACK)

    if not _weak(ans1):
        # ✅ 여기서 바로 FAQ 반영
        ans1_fixed = _maybe_override_with_faq_answer(question, ans1)
        hits1_fixed = _attach_faq_hits(question, hits1)
        return ans1_fixed, hits1_fixed

    # ─ 2차(키워드 확장) 검색 ────────────────────────────────
    try:
        kw = ask_gemini(
            "아래 질문의 한국어 핵심 키워드를 쉼표로 10개만. 설명 없이 키워드만:\n" + question,
            model=None,
        )
    except Exception:
        kw = ""

    expanded_q = (question + " " + (kw or "")).strip()

    res2 = _chroma_query_with_embeddings(col, expanded_q, fallback_topk, where=None)
    hits2_all = _parse_hits_from_res(res2)
    hits2 = _rank_and_dedupe_hits(hits2_all, max_sources)
    block2 = _build_source_block(hits2)

    ans2 = ask_gemini(_make_rag_prompt(question, block2, hard=not rag_force_answer), model=None)

    if not _weak(ans2):
        ans2_fixed = _maybe_override_with_faq_answer(question, ans2)
        hits2_fixed = _attach_faq_hits(question, hits2)
        return ans2_fixed, hits2_fixed

    # ─ 일반 지식 폴백 ───────────────────────────────────────
    if rag_force_answer and _weak(ans2):
        ans_fallback = ask_gemini(
            "다음 질문에 대해 일반 지식과 상식을 바탕으로 한국어로 4~8문장 핵심 요약 답을 작성하세요. "
            "군더더기 금지, 안전하고 중립적인 표현 사용:\n\n"
            f"{question}\n\n=== 답변 시작 ===\n",
            model=None,
        )
        if (ans_fallback or "").strip():
            ans_fb_fixed = _maybe_override_with_faq_answer(question, ans_fallback.strip())
            hits_fb_fixed = _attach_faq_hits(question, hits2 or hits1)
            return ans_fb_fixed, hits_fb_fixed

    # ─ 최종 완전 폴백 ───────────────────────────────────────
    final_ans = (ans2 or ans1 or _EMPTY_FALLBACK)
    final_hits = (hits2 or hits1)

    final_ans = _maybe_override_with_faq_answer(question, final_ans)
    final_hits = _attach_faq_hits(question, final_hits)

    return final_ans, final_hits



def _rerank_hits_by_relevance(question: str, hits: List[Dict[str, Any]], topn: int = 5):
    if not hits:
        return []
    # 거리(score)는 낮을수록 가까움 → 오름차순
    def score(h):
        s = h.get("score")
        try:
            return float(s) if s is not None else 1e9
        except Exception:
            return 1e9

    return sorted(hits, key=score)[:topn]


def _build_history_context(history: List[Dict[str, str]], max_turns: int = 3) -> str:
    if not history:
        return ""
    recent = history[-max_turns:]
    lines = []
    for turn in recent:
        q = (turn.get("q") or "").strip()
        a = (turn.get("a") or "").strip()
        if q:
            lines.append(f"User: {q}")
        if a:
            lines.append(f"Assistant: {a}")
    return "\n".join(lines)


def rag_answer_grounded_with_history(
    question: str,
    history: List[dict],
    *,
    base_retriever_func=rag_answer_grounded,
    initial_topk: int = 5,
    fallback_topk: int = 12,
    max_sources: int = 8,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    히스토리(최근 대화 몇 턴)를 간접적으로 참고하는 RAG.
    - 검색/생성/FAQ 처리 자체는 base_retriever_func(rag_answer_grounded)에 맡긴다.
    - 여기서는 hit 리스트를 relevance 기준으로 정리만.
    """
    answer_text, used_hits = base_retriever_func(
        question,
        initial_topk=initial_topk,
        fallback_topk=fallback_topk,
        max_sources=max_sources,
    )

    # 점수 기준으로 상위 몇 개만 정리
    final_hits = _rerank_hits_by_relevance(question, used_hits, topn=5)
    return answer_text, final_hits


__all__ = [
    # 생성/임베딩
    "ask_gemini",
    "_embed_texts",
    # 크롤/검색
    "fetch_article_text",
    "crawl_news_bodies",
    "search_news_rss",
    "gemini_answer_with_news",
    # 인덱싱/스토어
    "indexto_chroma_safe",
    "chroma_upsert",  # (전역) 호환 export
    # RAG
    "rag_answer_grounded",
    "rag_answer_grounded_with_history",
    # 헬퍼(호환)
    "_chunk_text",
    "_slug",
    "_sha",
    "_iso",
    "_chroma_collection",
    "_chroma_query_with_embeddings",
]
