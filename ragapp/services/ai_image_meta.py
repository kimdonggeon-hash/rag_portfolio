from __future__ import annotations

import json
import os
import re
import logging
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

_JSON_RE = re.compile(r"\{.*\}", re.S)

def _uniq_keep_order(xs: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for x in xs:
        x = (x or "").strip()
        if not x:
            continue
        k = x.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(x)
    return out

def _safe_json_extract(text: str) -> Optional[dict]:
    if not text:
        return None
    m = _JSON_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None

def _build_search_text(*, caption: str, tags: List[str], filename_hint: str = "") -> str:
    base = " ".join([caption or "", " ".join(tags or []), filename_hint or ""]).strip().lower()
    tokens = re.findall(r"[0-9a-zA-Z가-힣_\-]+", base)
    tokens = [t.strip().lower() for t in tokens if t and len(t.strip()) >= 2]
    tokens = _uniq_keep_order(tokens)
    return " ".join(tokens)

def _guess_mime(gcs_uri: str) -> str:
    u = (gcs_uri or "").lower()
    if u.endswith(".png"):
        return "image/png"
    if u.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"

def _vertex_gemini_caption(gcs_uri: str, model_name: str) -> Tuple[str, List[str], Dict[str, Any]]:
    """
    Vertex AI Gemini(vision)로 caption/tags 생성.
    - 너 프로젝트가 이미 vertexai를 쓰고 있으면 그대로 동작할 확률이 높음.
    """
    try:
        import vertexai
        try:
            from vertexai.generative_models import GenerativeModel, Part
        except Exception:
            # 일부 환경은 preview 경로일 수 있음
            from vertexai.preview.generative_models import GenerativeModel, Part  # type: ignore
    except Exception as e:
        raise RuntimeError(f"vertexai import 실패: {e}")

    project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or os.getenv("PROJECT_ID")
    location = os.getenv("VERTEX_LOCATION") or os.getenv("VERTEXAI_LOCATION") or "us-central1"
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT(또는 GCP_PROJECT) 환경변수가 필요합니다.")

    vertexai.init(project=project, location=location)

    prompt = (
        "너는 이미지 검색용 메타데이터를 만든다.\n"
        "아래 JSON만 출력하라(설명 문장 금지).\n"
        "{\n"
        '  "caption": "짧고 명확한 캡션(한국어 우선)",\n'
        '  "tags": ["검색 태그 8~20개, 한/영 혼합, 소문자 권장"]\n'
        "}\n"
        "- 로고/아이콘이면 브랜드명/로고/아이콘 포함\n"
        "- 화면 캡처면 핵심 UI/텍스트 키워드 포함\n"
    )

    m = GenerativeModel(model_name)
    img = Part.from_uri(gcs_uri, mime_type=_guess_mime(gcs_uri))
    resp = m.generate_content([img, prompt])
    text = getattr(resp, "text", "") or ""

    data = _safe_json_extract(text) or {}
    caption = (data.get("caption") or "").strip()
    tags = data.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    tags = [str(x).strip() for x in tags if x is not None]
    tags = _uniq_keep_order(tags)

    dbg = {"model": model_name, "location": location, "project": project, "raw": text[:1500], "json": data}
    return caption, tags, dbg

def enrich_meta_with_ai(
    *,
    meta: Dict[str, Any],
    gcs_uri: str,
    filename_hint: str = "",
    force: bool = False,
    model_name: str = "gemini-3.5-flash",
) -> Dict[str, Any]:
    """
    meta에 caption/tags/search_text/ai_captioned를 채움.
    - force=False면, 이미 채워져 있으면 스킵(비용 절약)
    """
    meta = dict(meta or {})

    need_ai = force or (not meta.get("ai_captioned")) or (not meta.get("caption")) or (not meta.get("search_text"))
    if not need_ai:
        return meta

    try:
        caption, tags, dbg = _vertex_gemini_caption(gcs_uri, model_name=model_name)
        if not caption:
            caption = (filename_hint or "이미지").strip() or "이미지"
        search_text = _build_search_text(caption=caption, tags=tags, filename_hint=filename_hint)

        meta["caption"] = caption
        meta["tags"] = tags
        meta["search_text"] = search_text
        meta["ai_captioned"] = 1
        meta["ai_model"] = dbg.get("model", "")
        return meta
    except Exception:
        # 실패해도 업로드/리인덱스가 죽으면 안 됨
        log.exception("AI meta 생성 실패: %s", gcs_uri)
        return meta
