# ragapp/management/commands/media_reindex_storage_images.py
from __future__ import annotations

import os
import re
import json
import time
import mimetypes
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

from django.core.management.base import BaseCommand
from django.core.files.storage import default_storage

from ragapp.services.vertex_embed import embed_image_file
from ragapp.services.chroma_media import images_coll, _pid_for_key  # ✅ ID 체계 통일

log = logging.getLogger(__name__)

_VERTEX_INITED = False


def _walk_storage(prefix: str):
    """
    default_storage(FileSystem/GCS 공통)에서 prefix 아래 파일을 재귀적으로 열거
    """
    prefix = (prefix or "").strip().lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    stack = [prefix]
    while stack:
        cur = stack.pop()
        try:
            dirs, files = default_storage.listdir(cur)
        except Exception:
            continue

        for f in files:
            key = (cur + f).lstrip("/")
            yield key

        for d in dirs:
            nxt = (cur + d).rstrip("/") + "/"
            stack.append(nxt)


# ------------------------------
# AI 메타 생성 유틸 (caption/tags/search_text)
# ------------------------------
_JSON_RE = re.compile(r"\{.*\}", re.S)


def _safe_json_extract(text: str) -> Dict[str, Any]:
    if not text:
        return {}
    m = _JSON_RE.search(text)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


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


def _build_search_text(caption: str, tags: List[str], filename_hint: str) -> str:
    base = " ".join([caption or "", " ".join(tags or []), filename_hint or ""]).strip().lower()
    toks = re.findall(r"[0-9a-zA-Z가-힣_\-]+", base)
    toks = [t.strip().lower() for t in toks if t and len(t.strip()) >= 2]
    toks = _uniq_keep_order(toks)
    return " ".join(toks)


def _vertex_init() -> None:
    """
    Vertex AI init을 1회만 수행.
    - 프로젝트는 VERTEX_PROJECT_ID만 사용(없으면 에러)
    """
    global _VERTEX_INITED
    if _VERTEX_INITED:
        return

    try:
        import vertexai  # noqa: F401
    except Exception as e:
        raise RuntimeError(f"vertexai import 실패: {e}")

    import vertexai

    # ✅ GOOGLE_CLOUD_PROJECT / GCP_PROJECT / PROJECT_ID 완전 제거
    project = (os.getenv("VERTEX_PROJECT_ID") or "").strip()

    location = (
        (os.getenv("VERTEX_LOCATION") or "").strip()
        or (os.getenv("GOOGLE_CLOUD_LOCATION") or "").strip()
        or (os.getenv("VERTEXAI_LOCATION") or "").strip()
        or "us-central1"
    )

    if not project:
        raise RuntimeError("VERTEX_PROJECT_ID 환경변수가 필요합니다.")

    vertexai.init(project=project, location=location)
    _VERTEX_INITED = True


def _ai_make_meta_from_file(
    *,
    file_path: Path,
    mime: str,
    filename_hint: str,
    model_name: str,
) -> Tuple[str, List[str], str]:
    """
    이미지 파일(tmp)을 Vertex Gemini Vision에 넣고:
    - caption (str)
    - tags (List[str])
    - search_text (str)
    를 반환
    """
    _vertex_init()

    try:
        from vertexai.generative_models import GenerativeModel, Part
    except Exception:
        # 일부 환경은 preview 경로일 수 있음
        from vertexai.preview.generative_models import GenerativeModel, Part  # type: ignore

    prompt = (
        "너는 이미지 검색용 메타데이터를 만든다.\n"
        "아래 JSON만 출력하라(설명 문장 금지).\n"
        "{\n"
        '  "caption": "짧고 명확한 캡션(한국어 우선)",\n'
        '  "tags": ["검색 태그 8~20개, 한/영 혼합, 소문자 권장"]\n'
        "}\n"
        "- 로고/아이콘이면 브랜드명/로고/아이콘 키워드를 포함하라.\n"
        "- 화면 캡처면 핵심 UI/텍스트 키워드를 포함하라.\n"
    )

    data = file_path.read_bytes()
    img = Part.from_data(data=data, mime_type=mime)

    m = GenerativeModel(model_name)
    resp = m.generate_content([img, prompt])
    text = getattr(resp, "text", "") or ""

    j = _safe_json_extract(text)

    caption = (j.get("caption") or "").strip()
    tags = j.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    tags = [str(x).strip() for x in tags if x is not None]
    tags = _uniq_keep_order(tags)

    if not caption:
        caption = (filename_hint or "이미지").strip() or "이미지"

    search_text = _build_search_text(caption, tags, filename_hint)
    return caption, tags, search_text


def _need_ai(meta: Dict[str, Any], *, force: bool) -> bool:
    if force:
        return True
    if not meta.get("ai_captioned"):
        return True
    if not meta.get("caption"):
        return True
    if not meta.get("search_text"):
        return True
    return False


def _normalize_tags_to_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    """
    meta["tags"]를 서비스 규격에 맞게 정규화:
    - tags: "a b c" (문자열)
    - tags_list: ["a","b","c"] (리스트)
    """
    raw = meta.get("tags")
    if isinstance(raw, list):
        tags_list = [str(x).strip() for x in raw if x is not None and str(x).strip()]
    else:
        # 공백/콤마 혼용 대비
        s = (str(raw) if raw is not None else "").strip()
        tags_list = [t.strip() for t in re.split(r"[,\s]+", s) if t.strip()]

    tags_list = _uniq_keep_order(tags_list)
    tags_str = " ".join(tags_list).strip()

    meta["tags_list"] = tags_list[:30]
    meta["tags"] = tags_str
    return meta


class Command(BaseCommand):
    help = "Re-index images already stored in default_storage (GCS/FS) into Chroma media_images."

    def add_arguments(self, parser):
        parser.add_argument("--prefix", type=str, default="images/", help="Storage prefix to scan (default: images/)")
        parser.add_argument("--apply", action="store_true", help="Actually write to Chroma. Default is dry-run.")
        parser.add_argument("--limit", type=int, default=0, help="Stop after N files (0 = no limit).")
        parser.add_argument("--skip-existing", action="store_true", help="Skip keys already present in Chroma metadata.")
        parser.add_argument("--caption-from-name", action="store_true", help="Use filename stem as caption (fallback).")

        # ✅ AI 메타 생성 옵션
        parser.add_argument("--ai", action="store_true", help="Generate AI caption/tags/search_text using Vertex Gemini.")
        parser.add_argument(
            "--ai-model",
            type=str,
            default=os.getenv("IMAGE_META_MODEL") or "gemini-3.5-flash",
            help="Gemini model name.",
        )
        parser.add_argument("--ai-force", action="store_true", help="Force regenerate AI meta even if already exists.")

        # ✅ 기존 이미지 메타만 채우기(임베딩 재계산 X)
        parser.add_argument(
            "--meta-only",
            action="store_true",
            help="Only update metadata/documents for existing items (no embedding).",
        )

        # ✅ 레이트리밋/비용 제어
        parser.add_argument("--sleep", type=float, default=0.0, help="Sleep seconds between AI calls (0 = no sleep).")

    def handle(self, *args, **opts):
        prefix = opts["prefix"]
        apply = bool(opts["apply"])
        limit = max(0, int(opts["limit"]))
        skip_existing = bool(opts["skip_existing"])
        caption_from_name = bool(opts["caption_from_name"])

        ai_enabled = bool(opts["ai"])
        ai_model = str(opts["ai_model"] or "gemini-3.5-flash")
        ai_force = bool(opts["ai_force"])
        meta_only = bool(opts["meta_only"])
        sleep_sec = float(opts["sleep"] or 0.0)

        mode_str = "APPLY" if apply else "DRY-RUN"
        self.stdout.write(
            self.style.WARNING(
                f"[media_reindex_storage_images] mode={mode_str} prefix={prefix!r} "
                f"ai={'ON' if ai_enabled else 'OFF'} meta_only={'YES' if meta_only else 'NO'}"
            )
        )

        # meta-only인데 skip-existing 켜면 “전부 스킵”이 돼서 의미가 없어짐
        if meta_only and skip_existing:
            self.stdout.write(
                self.style.WARNING(
                    "[warn] --meta-only 사용 시 --skip-existing은 무의미합니다(기존만 업데이트). 무시합니다."
                )
            )
            skip_existing = False

        # ✅ 기존 Chroma 데이터 로드: path -> {id, meta}
        existing: Dict[str, Dict[str, Any]] = {}
        # skip-existing이든, meta-only든, ai든 “기존 여부/메타 확인”이 필요할 수 있어서 로딩
        need_existing_scan = skip_existing or meta_only or ai_enabled
        if need_existing_scan:
            c = images_coll()
            off = 0
            batch = 500
            while True:
                got = c.get(include=["metadatas", "documents"], limit=batch, offset=off)
                ids = got.get("ids") or []
                metas = got.get("metadatas") or []
                if not ids:
                    break

                for _id, m in zip(ids, metas):
                    if not isinstance(m, dict):
                        continue
                    p = (m.get("path") or m.get("storage_key") or m.get("filepath") or "").strip()
                    if not p:
                        continue
                    pn = p.replace("\\", "/")
                    existing[pn] = {"id": _id, "meta": m}

                off += batch

            self.stdout.write(f"Loaded existing items: {len(existing)}")

        done = 0
        indexed = 0
        skipped = 0
        failed = 0
        updated_meta = 0

        tmp_dir = (
            Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp")
            / "ragapp_reindex_tmp"
        )
        try:
            tmp_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        coll = images_coll()

        for key in _walk_storage(prefix):
            if limit and done >= limit:
                break
            done += 1

            key_norm = key.replace("\\", "/")
            ex = existing.get(key_norm)
            ex_id = (ex or {}).get("id")
            ex_meta = (ex or {}).get("meta") if isinstance((ex or {}).get("meta"), dict) else None

            # ✅ skip-existing: 기존에 있으면 완전 스킵
            if skip_existing and ex_id:
                skipped += 1
                continue

            tmp_path: Optional[Path] = None
            try:
                # storage -> tmp file (AI를 쓰든, 임베딩을 하든, 둘 다 여기서 필요)
                suffix = Path(key_norm).suffix or ".bin"
                tmp_path = tmp_dir / f"reindex_{done}{suffix}"

                with default_storage.open(key_norm, "rb") as rf, open(tmp_path, "wb") as wf:
                    while True:
                        chunk = rf.read(1024 * 1024)
                        if not chunk:
                            break
                        wf.write(chunk)

                mime = mimetypes.guess_type(key_norm)[0] or "application/octet-stream"

                # ------------------------------
                # ✅ 메타 구성 지점
                # ------------------------------
                meta: Dict[str, Any] = dict(ex_meta or {})
                meta["path"] = key_norm
                meta["filepath"] = key_norm
                meta["storage_key"] = key_norm
                meta["mime"] = mime
                meta.setdefault("original_name", Path(key_norm).name)

                filename_hint = meta.get("original_name") or Path(key_norm).stem or key_norm

                # 1) 기본 caption (파일명 기반 폴백)
                if caption_from_name and not meta.get("caption"):
                    meta["caption"] = Path(key_norm).stem

                # 2) ✅ AI 메타 생성 (caption/tags/search_text)
                if ai_enabled and _need_ai(meta, force=ai_force):
                    cap, tags, search_text = _ai_make_meta_from_file(
                        file_path=tmp_path,
                        mime=mime,
                        filename_hint=str(filename_hint),
                        model_name=ai_model,
                    )
                    meta["caption"] = cap
                    meta["tags"] = tags  # 일단 raw로 넣고 아래에서 정규화
                    meta["search_text"] = search_text
                    meta["ai_captioned"] = 1
                    meta["ai_model"] = ai_model
                    if sleep_sec > 0:
                        time.sleep(sleep_sec)

                # 3) ✅ tags/tags_list 정규화(서비스 규격 통일)
                _normalize_tags_to_meta(meta)

                # 4) documents는 search_text 우선(없으면 caption)
                doc = (meta.get("search_text") or meta.get("caption") or "").strip()

                # ------------------------------
                # ✅ Chroma에 쓰는 지점
                # ------------------------------
                if apply:
                    if meta_only:
                        # meta-only는 “기존 id가 있을 때만” update 가능
                        if not ex_id:
                            skipped += 1
                            continue

                        coll.update(
                            ids=[ex_id],
                            metadatas=[meta],
                            documents=[doc],
                        )
                        updated_meta += 1

                    else:
                        vec = embed_image_file(str(tmp_path), mime=mime)

                        if ex_id:
                            # 기존 항목이면 같은 id로 update (중복 방지)
                            coll.update(
                                ids=[ex_id],
                                embeddings=[vec],
                                metadatas=[meta],
                                documents=[doc],
                            )
                        else:
                            # ✅ 신규 항목도 서비스와 동일한 ID 규칙 사용 (중복/혼용 방지)
                            new_id = _pid_for_key(key_norm)
                            try:
                                coll.add(
                                    ids=[new_id],
                                    embeddings=[vec],
                                    metadatas=[meta],
                                    documents=[doc],
                                )
                            except Exception:
                                # 혹시 같은 id 충돌이면 update로 폴백
                                coll.update(
                                    ids=[new_id],
                                    embeddings=[vec],
                                    metadatas=[meta],
                                    documents=[doc],
                                )

                        indexed += 1

                else:
                    # DRY-RUN: 실제 쓰지 않음
                    indexed += 1

                if done % 10 == 0:
                    self.stdout.write(
                        f"... processed={done} indexed={indexed} updated_meta={updated_meta} "
                        f"skipped={skipped} failed={failed}"
                    )

            except Exception as e:
                failed += 1
                self.stdout.write(self.style.ERROR(f"FAIL: {key_norm} -> {e.__class__.__name__}: {e}"))
            finally:
                if tmp_path is not None:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except Exception:
                        pass

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. scanned={done}, indexed={indexed}, updated_meta={updated_meta}, skipped={skipped}, failed={failed}"
            )
        )
