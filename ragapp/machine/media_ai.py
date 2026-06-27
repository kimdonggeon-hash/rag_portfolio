# ragapp/machine/media_ai.py
from __future__ import annotations

import os
import re
import json
from typing import Tuple

from .feature_config import truthy_env

# =========================================================
# ✅ AI 메타 강화(캡션/태그)
# =========================================================
PUBLIC_IMAGE_UPLOAD_AI = truthy_env("PUBLIC_IMAGE_UPLOAD_AI", "0")

IMAGE_AI_MODEL = (os.getenv("IMAGE_AI_MODEL") or "gemini-3.5-flash").strip()
IMAGE_AI_LANG = (os.getenv("IMAGE_AI_LANG") or "ko").strip()  # "ko" / "en"
IMAGE_AI_MAX_TAGS = int((os.getenv("IMAGE_AI_MAX_TAGS") or "12").strip() or 12)
IMAGE_AI_FALLBACK_PLAIN_CAPTION = truthy_env("IMAGE_AI_FALLBACK_PLAIN_CAPTION", "1")


def _split_tags(s: str) -> list[str]:
    raw = (s or "").strip()
    if not raw:
        return []
    parts = re.split(r"[,\n\r\t|/]+|\s{2,}", raw)
    out: list[str] = []
    for p in parts:
        t = (p or "").strip()
        if not t:
            continue
        out.append(t)
    return out


def _merge_tags(a: str, b: str, max_n: int = 12) -> str:
    seen = set()
    merged: list[str] = []
    for t in _split_tags(a) + _split_tags(b):
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        merged.append(t)
        if len(merged) >= max_n:
            break
    return ", ".join(merged)


def _extract_json_object(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"\s*```$", "", s).strip()
    m = re.search(r"\{.*\}", s, flags=re.DOTALL)
    return (m.group(0).strip() if m else "")


def ai_caption_tags_from_image(*, file_path: str, mime: str) -> Tuple[str, str, dict]:
    """
    이미지 파일에서 캡션/태그를 AI로 생성.
    반환: (caption, tags_csv, ai_meta)
    - 실패 시 ("", "", {"ai_error": ...}) 형태
    """
    if not PUBLIC_IMAGE_UPLOAD_AI:
        return "", "", {"ai_used": False}

    try:
        try:
            if "_vertex_init" in globals() and callable(globals()["_vertex_init"]):
                globals()["_vertex_init"]()
        except Exception:
            pass

        from vertexai.generative_models import GenerativeModel, Part  # type: ignore

        model = GenerativeModel(IMAGE_AI_MODEL)

        with open(file_path, "rb") as rf:
            data = rf.read()

        img_part = Part.from_data(data=data, mime_type=(mime or "image/jpeg"))

        if IMAGE_AI_LANG.lower().startswith("ko"):
            prompt = f"""
너는 이미지 메타데이터 생성기다.
다음 이미지에 대해:
1) 한 문장 캡션(caption)을 한국어로 작성
2) 검색에 유용한 태그(tags)를 {IMAGE_AI_MAX_TAGS}개 이내로 작성(짧은 명사/구)
반드시 아래 JSON만 출력해라(다른 텍스트 금지):
{{"caption": "...", "tags": ["...", "..."]}}
"""
        else:
            prompt = f"""
You are an image metadata generator.
For the given image:
1) Write a one-sentence caption in English.
2) Provide up to {IMAGE_AI_MAX_TAGS} short search-friendly tags.
Output ONLY this JSON (no extra text):
{{"caption": "...", "tags": ["...", "..."]}}
"""

        resp = model.generate_content([prompt, img_part])
        raw = (getattr(resp, "text", "") or "").strip()

        js = _extract_json_object(raw)
        if js:
            obj = json.loads(js)
            cap = (obj.get("caption") or "").strip()
            tags = obj.get("tags") or []

            if isinstance(tags, str):
                tags_list = _split_tags(tags)
            elif isinstance(tags, list):
                tags_list = [str(x).strip() for x in tags if str(x).strip()]
            else:
                tags_list = []

            tags_list = tags_list[: max(1, IMAGE_AI_MAX_TAGS)]
            tags_csv = ", ".join(tags_list)

            return cap, tags_csv, {"ai_used": True, "ai_model": IMAGE_AI_MODEL}

        if IMAGE_AI_FALLBACK_PLAIN_CAPTION and raw:
            cap = raw.splitlines()[0].strip()
            return cap, "", {
                "ai_used": True,
                "ai_model": IMAGE_AI_MODEL,
                "ai_parse": "fallback_plain_caption",
            }

        return "", "", {"ai_used": False, "ai_error": "ai_json_parse_failed"}

    except Exception as e:
        return "", "", {"ai_used": False, "ai_error": f"{e.__class__.__name__}: {e}"}
