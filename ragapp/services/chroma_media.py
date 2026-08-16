# ragapp/services/chroma_media.py
from __future__ import annotations

import os
import re
import json
import mimetypes
import hashlib
import time
import shutil
import tempfile
import logging
import threading
from functools import lru_cache
from pathlib import Path
from typing import List, Dict, Any, Optional, Union, Tuple

from django.core.files.storage import default_storage

log = logging.getLogger(__name__)

CHROMA_MEDIA_DIR = os.getenv("CHROMA_MEDIA_DIR", "chroma_media")

MM_FIXED_DIM = 1408  # multimodalembedding@001 고정 (이미지 임베딩 차원)

# -----------------------------------------------------------------------------
# Sync 정책(핵심)
# -----------------------------------------------------------------------------
# ✅ 쓰기 발생 시 remote(/mnt/gcs/...)로 자동 sync_up (캡션/태그 “배포 후 초기화” 방지)
# - CHROMA_SYNC_UP_ON_WRITE=1 (default)
# - CHROMA_SYNC_UP_MIN_INTERVAL=15 (초) : 너무 자주 sync 하지 않도록 스로틀
# ✅ reindex/backfill이 빈 값으로 덮어쓰지 않도록 기존 메타 보존
# - CHROMA_IMAGE_META_PRESERVE=1 (default)
_SYNC_LOCK = threading.Lock()
_LAST_SYNC_UP_AT = 0.0


# -----------------------------------------------------------------------------
# 기본 유틸
# -----------------------------------------------------------------------------
def _env_bool(key: str, default: bool = False) -> bool:
    v = (os.getenv(key, "") or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _safe_int(v: str, default: int) -> int:
    try:
        return int(str(v).strip())
    except Exception:
        return default


def _is_mounted_gcs_path(p: Path) -> bool:
    s = str(p).replace("\\", "/")
    return s.startswith("/mnt/gcs/")


def _should_skip_sync_file(name: str) -> bool:
    n = (name or "").lower()
    return (
        n.endswith(".sqlite3-journal")
        or n.endswith(".sqlite3-wal")
        or n.endswith(".sqlite3-shm")
        or n.endswith("-journal")
        or n.endswith(".journal")
        or n.endswith(".wal")
        or n.endswith(".shm")
    )


def _dir_has_chroma_files(p: Path) -> bool:
    if not p.exists() or not p.is_dir():
        return False
    if (p / "chroma.sqlite3").exists():
        return True
    for f in p.rglob("*"):
        if f.is_file() and f.name.endswith(".bin"):
            return True
    return False


def _dir_mtime(p: Path) -> float:
    try:
        db = p / "chroma.sqlite3"
        if db.exists():
            return db.stat().st_mtime
    except Exception:
        pass

    newest = 0.0
    try:
        for f in p.rglob("*"):
            if f.is_file():
                newest = max(newest, f.stat().st_mtime)
    except Exception:
        pass
    return newest or 0.0


def _sync_down_if_needed(*, remote_dir: str, local_dir: str, max_age_sec: int) -> None:
    r = Path(remote_dir)
    l = Path(local_dir)

    if not remote_dir:
        return
    if not r.exists() or not r.is_dir():
        return

    # LOCAL이 이미 있고 최신이면 스킵
    if _dir_has_chroma_files(l):
        age = time.time() - _dir_mtime(l)
        if age >= 0 and age <= max_age_sec:
            return

    tmp = Path(str(l) + ".__sync_tmp__")
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    for src in r.rglob("*"):
        rel = src.relative_to(r)
        dst = tmp / rel
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            continue
        if _should_skip_sync_file(src.name):
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)

    if l.exists():
        shutil.rmtree(l, ignore_errors=True)
    tmp.rename(l)


def sync_media_up(*, local_dir: Optional[str] = None, remote_dir: Optional[str] = None, prune: bool = False) -> str:
    local_dir = (local_dir or os.getenv("CHROMA_MEDIA_DIR", "/tmp/chroma_media")).strip()
    remote_dir = (remote_dir or os.getenv("CHROMA_MEDIA_REMOTE_DIR", "")).strip()

    l = Path(local_dir)
    r = Path(remote_dir)

    if not remote_dir:
        return "[sync_up] remote_dir empty (CHROMA_MEDIA_REMOTE_DIR is not set)"
    if not _dir_has_chroma_files(l):
        return f"[sync_up] local empty/not found: {l}"

    r.mkdir(parents=True, exist_ok=True)
    touched: set[Path] = set()

    for src in l.rglob("*"):
        rel = src.relative_to(l)
        dst = r / rel

        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            continue
        if _should_skip_sync_file(src.name):
            continue

        dst.parent.mkdir(parents=True, exist_ok=True)

        # ✅ /mnt/gcs (gcsfuse) 에서는 tmp/replace/chmod/copy2 금지 → direct overwrite copy
        if _is_mounted_gcs_path(dst):
            try:
                shutil.copyfile(src, dst)  # 내용만, 메타데이터 조작 없음
                touched.add(dst)
            except Exception:
                pass
            continue

        # ✅ 로컬(또는 gcsfuse가 아닌 경로)에서는 원자적 교체 유지
        tmp_dst = dst.with_name(dst.name + f".__tmp__{os.getpid()}_{int(time.time()*1000)}")
        try:
            try:
                if tmp_dst.exists():
                    tmp_dst.unlink()
            except Exception:
                pass

            try:
                shutil.copy2(src, tmp_dst)
            except Exception:
                shutil.copyfile(src, tmp_dst)

            os.replace(tmp_dst, dst)
            touched.add(dst)

        finally:
            try:
                if tmp_dst.exists():
                    tmp_dst.unlink()
            except Exception:
                pass

    if prune:
        for dst in r.rglob("*"):
            if dst.is_file() and dst not in touched and not _should_skip_sync_file(dst.name):
                try:
                    dst.unlink()
                except Exception:
                    pass

    return f"[sync_up] updated {r} from {l} (prune={prune})"


def _maybe_sync_up_after_write(reason: str = "") -> None:
    """
    ✅ 쓰기 발생 후 remote로 DB를 밀어 넣어 “배포/트래픽 이후 캡션 초기화”를 방지.
    - 너무 자주 sync 하지 않도록 스로틀
    - remote가 없으면 noop
    """
    if not _env_bool("CHROMA_SYNC_UP_ON_WRITE", True):
        return

    remote_dir = (os.getenv("CHROMA_MEDIA_REMOTE_DIR", "") or "").strip()
    if not remote_dir:
        return

    interval = _safe_int(os.getenv("CHROMA_SYNC_UP_MIN_INTERVAL", "15"), 15)
    local_dir = (os.getenv("CHROMA_MEDIA_DIR", "/tmp/chroma_media") or "/tmp/chroma_media").strip()

    global _LAST_SYNC_UP_AT
    now = time.time()

    # 스로틀 + 동시성 보호
    with _SYNC_LOCK:
        if _LAST_SYNC_UP_AT and (now - _LAST_SYNC_UP_AT) < max(0, interval):
            return

    try:
        msg = sync_media_up(local_dir=local_dir, remote_dir=remote_dir, prune=False)
        with _SYNC_LOCK:
            _LAST_SYNC_UP_AT = time.time()
        if reason:
            log.info("[chroma sync_up] (%s) %s", reason, msg)
        else:
            log.info("[chroma sync_up] %s", msg)
    except Exception:
        log.exception("[chroma sync_up] failed (reason=%s)", reason)

_SYNC_UP_LOCK = threading.Lock()
_LAST_SYNC_UP_TS = 0.0

def _maybe_sync_up(reason: str = "") -> None:
    """
    메타/벡터 DB가 변경된 뒤 remote_dir로 sync_up.
    - 서비스에서 매 요청마다 돌면 무거우니, env로 on/off + 최소 간격 둔다.
    """
    if not _env_bool("CHROMA_SYNC_UP_ON_WRITE", False):
        return

    remote_dir = (os.getenv("CHROMA_MEDIA_REMOTE_DIR", "") or "").strip()
    if not remote_dir:
        return

    local_dir = (os.getenv("CHROMA_MEDIA_DIR", "/tmp/chroma_media") or "/tmp/chroma_media").strip()
    min_interval = _safe_int(os.getenv("CHROMA_SYNC_UP_MIN_INTERVAL", "15"), 15)
    prune = _env_bool("CHROMA_SYNC_UP_PRUNE", False)

    now = time.time()
    global _LAST_SYNC_UP_TS
    with _SYNC_UP_LOCK:
        if (now - _LAST_SYNC_UP_TS) < float(min_interval):
            return
        _LAST_SYNC_UP_TS = now

    try:
        sync_media_up(local_dir=local_dir, remote_dir=remote_dir, prune=prune)
    except Exception:
        log.exception("[sync_up] failed reason=%s", reason)

# -----------------------------------------------------------------------------
# Chroma Client / Collections
# -----------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _client():
    import chromadb

    media_dir = (os.getenv("CHROMA_MEDIA_DIR", "") or CHROMA_MEDIA_DIR or "chroma_media").strip()
    remote_dir = (os.getenv("CHROMA_MEDIA_REMOTE_DIR", "") or "").strip()
    sync_max_age = _safe_int(os.getenv("CHROMA_SYNC_MAX_AGE", "600"), 600)

    p = Path(media_dir)

    # /mnt/gcs에 Chroma 직접 쓰기 차단
    if _is_mounted_gcs_path(p):
        raise RuntimeError(
            "CHROMA_MEDIA_DIR이 /mnt/gcs 아래로 설정되어 있습니다. "
            "Cloud Storage FUSE(/mnt/gcs)는 SQLite/Chroma에 부적합할 수 있습니다. "
            "권장: CHROMA_MEDIA_DIR=/tmp/chroma_media, CHROMA_MEDIA_REMOTE_DIR=/mnt/gcs/ragdata/chroma_media"
        )

    # remote -> local 동기화
    if remote_dir:
        _sync_down_if_needed(remote_dir=remote_dir, local_dir=str(p), max_age_sec=sync_max_age)

    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise RuntimeError(
            f"CHROMA_MEDIA_DIR 경로 생성 실패: {media_dir}. "
            f"원인: {e.__class__.__name__}: {e}"
        ) from e

    try:
        return chromadb.PersistentClient(path=str(p))
    except TypeError:
        from chromadb.config import Settings
        return chromadb.PersistentClient(settings=Settings(persist_directory=str(p)))


def _detect_existing_dim(col: Any) -> Optional[int]:
    """
    컬렉션에 기존 데이터가 있으면 embeddings 길이로 차원을 추정한다.
    (없으면 None)
    """
    try:
        got = col.get(include=["embeddings"], limit=1)
        embs = got.get("embeddings")
        if embs is None or len(embs) == 0:
            return None
        first = embs[0]
        if first is not None:
            return int(len(first))
    except Exception:
        pass
    return None


@lru_cache(maxsize=1)
def images_coll():
    cli = _client()

    # ✅ 이미지 컬렉션은 오직 이 이름만 사용
    name = (os.getenv("CHROMA_IMAGES_COLLECTION", "") or "").strip() or "media_images"

    col = cli.get_or_create_collection(
        name=name,
        metadata={
            "hnsw:space": "cosine",
            "kind": "image",
            "dim": MM_FIXED_DIM,
        },
    )

    col_name = (getattr(col, "name", "") or "").strip()
    if col_name == "media":
        raise RuntimeError(
            "images_coll()이 'media' 컬렉션을 가리키고 있습니다. "
            "이미지 전용 컬렉션(CHROMA_IMAGES_COLLECTION=media_images)을 사용해야 합니다."
        )

    existing_dim = _detect_existing_dim(col)
    if existing_dim is not None and int(existing_dim) != MM_FIXED_DIM:
        raise RuntimeError(
            f"이미지 컬렉션 '{col_name}'의 기존 embedding 차원이 {existing_dim} 입니다. "
            f"이미지는 {MM_FIXED_DIM} 이어야 합니다. "
            "해결: 해당 컬렉션을 삭제하고(로컬이면 chroma 폴더/컬렉션 삭제), reindex를 다시 실행하세요."
        )

    return col


# ✅ 예전 코드 호환용
_images_collection = images_coll


def images_space() -> str:
    """
    media_search_view에서 distance->similarity 변환에 쓰는 'space' 값 제공.
    """
    try:
        col = images_coll()
        md = getattr(col, "metadata", None) or {}
        sp = (md.get("hnsw:space") or md.get("space") or "cosine")
        return str(sp)
    except Exception:
        return "cosine"


@lru_cache(maxsize=1)
def table_coll():
    return _client().get_or_create_collection(name="table_rows")


def table_embedding_dim() -> Optional[int]:
    """Return the persisted table vector size, if the collection has rows."""
    return _detect_existing_dim(table_coll())


# -----------------------------------------------------------------------------
# Storage helpers
# -----------------------------------------------------------------------------
def _guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _sha256_storage(name: str) -> Optional[str]:
    try:
        if not default_storage.exists(name):
            return None
        h = hashlib.sha256()
        with default_storage.open(name, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _storage_size(name: str) -> int:
    try:
        return int(default_storage.size(name))
    except Exception:
        return 0


def _storage_mtime(name: str) -> int:
    try:
        dt = default_storage.get_modified_time(name)  # type: ignore[attr-defined]
        if dt is not None:
            return int(dt.timestamp())
    except Exception:
        pass
    return int(time.time())


def _norm_text(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("_", " ").replace("-", " ").replace("/", " ").replace("\\", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _parse_tags(tags: Union[str, List[str], None]) -> List[str]:
    if tags is None:
        return []
    if isinstance(tags, list):
        out: List[str] = []
        seen = set()
        for t in tags:
            tt = _norm_text(str(t))
            if not tt:
                continue
            if tt in seen:
                continue
            seen.add(tt)
            out.append(tt)
        return out

    raw = _norm_text(str(tags))
    if not raw:
        return []
    parts = re.split(r"[,\s]+", raw)
    out: List[str] = []
    seen = set()
    for p in parts:
        pp = _norm_text(p)
        if not pp:
            continue
        if pp in seen:
            continue
        seen.add(pp)
        out.append(pp)
    return out


def _meta_scalar(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:
        return str(v)


def _pid_for_key(key: str) -> str:
    key = (key or "").strip().replace("\\", "/").lstrip("/")
    fid = _sha256_storage(key) or hashlib.sha256(key.encode("utf-8", "ignore")).hexdigest()
    size = _storage_size(key)
    return f"img:{fid}:{int(size)}"


def _build_search_text(caption: str, original_name: str, tags_str: str, base_stem: str) -> str:
    parts: List[str] = []
    if caption:
        parts.append(caption)
    if original_name:
        parts.append(original_name)
    if tags_str:
        parts.append(tags_str)
    if base_stem:
        parts.append(base_stem)
    return _norm_text(" ".join(parts))


def _download_storage_to_tmp(key: str) -> Tuple[str, str]:
    key = (key or "").strip().replace("\\", "/").lstrip("/")
    suffix = Path(key).suffix or ".bin"
    mime = _guess_mime(key)

    fd, tmp_path = tempfile.mkstemp(prefix="rag_media_", suffix=suffix)
    os.close(fd)

    with default_storage.open(key, "rb") as rf, open(tmp_path, "wb") as wf:
        while True:
            chunk = rf.read(1024 * 1024)
            if not chunk:
                break
            wf.write(chunk)

    return tmp_path, mime


# ─────────────────────────────────────────────────────────────
# AI caption/tags (Vertex Gemini Vision) - optional
# ─────────────────────────────────────────────────────────────
def _ai_enabled(ai: Optional[bool]) -> bool:
    """
    ai 파라미터가 명시되면 그 값을 우선.
    아니면 env로 제어(둘 중 하나라도 켜져 있으면 True).
    """
    if ai is not None:
        return bool(ai)
    return _env_bool("IMAGE_META_AI", False) or _env_bool("MEDIA_IMAGE_AI_ENABLED", False)


@lru_cache(maxsize=1)
def _vertex_init_once() -> bool:
    """
    vertexai.init을 1회만 시도. 실패하면 False 반환.
    - 프로젝트는 VERTEX_PROJECT_ID만 사용(없으면 실패)
    """
    try:
        import vertexai  # type: ignore
    except Exception:
        return False

    # ✅ GOOGLE_CLOUD_PROJECT 완전 제거
    project = (os.getenv("VERTEX_PROJECT_ID") or "").strip()
    if not project:
        return False

    location = (
        (os.getenv("VERTEX_LOCATION") or "").strip()
        or (os.getenv("GOOGLE_CLOUD_LOCATION") or "").strip()
        or (os.getenv("VERTEXAI_LOCATION") or "").strip()
        or "asia-northeast3"
    )

    try:
        vertexai.init(project=project, location=location)
        return True
    except Exception:
        return False


def _detect_bucket_name() -> str:
    """
    env 우선 -> django-storages(GCS)면 default_storage.bucket.name
    """
    for k in ("GCS_BUCKET_NAME", "UPLOADS_BUCKET", "GCS_BUCKET", "GS_BUCKET", "DJANGO_GCS_BUCKET_NAME"):
        v = (os.getenv(k, "") or "").strip()
        if v:
            return v

    try:
        b = getattr(default_storage, "bucket", None)
        name = getattr(b, "name", "") if b is not None else ""
        if name:
            return str(name)
    except Exception:
        pass

    return ""


def _gcs_uri_for_key(key: str) -> str:
    if (key or "").startswith("gs://"):
        return str(key)
    bucket = _detect_bucket_name()
    if not bucket:
        return ""
    return f"gs://{bucket}/{str(key).lstrip('/')}"


def _strip_code_fences(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _safe_json_extract_from_model(text: str) -> Dict[str, Any]:
    """
    모델 출력에서 JSON 객체만 안전하게 추출.
    """
    text = _strip_code_fences(text)
    if not text:
        return {}

    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass

    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _uniq_keep_order(items: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for x in items:
        t = (x or "").strip()
        if not t:
            continue
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def _ai_caption_tags_for_storage_key(
    *,
    key: str,
    mime: str,
    model_name: str = "",
    max_tags: int = 12,
) -> Tuple[str, List[str], str]:
    """
    반환: (caption, tags_list, used_model)
    - 가능하면 gs:// URI로 vision 입력
    - 안 되면 스토리지 -> tmp -> bytes로 입력
    """
    if not _vertex_init_once():
        return "", [], ""

    used_model = (
        (model_name or "").strip()
        or (os.getenv("IMAGE_META_MODEL") or "").strip()
        or (os.getenv("VERTEX_GEMINI_VISION_MODEL") or "").strip()
        or (os.getenv("VERTEX_GEMINI_MODEL") or "").strip()
        or "gemini-3.5-flash"
    )

    # vertexai import 경로 호환
    try:
        from vertexai.generative_models import GenerativeModel, Part  # type: ignore
    except Exception:
        try:
            from vertexai.preview.generative_models import GenerativeModel, Part  # type: ignore
        except Exception:
            return "", [], used_model

    prompt = (
        "너는 이미지에 대한 검색 메타데이터를 만든다.\n"
        "아래 이미지에서 핵심 내용만 뽑아서 JSON으로만 출력해라.\n"
        "형식:\n"
        "{\n"
        '  "caption": "한 문장(한국어)로 이미지 설명",\n'
        '  "tags": ["명사 위주 태그 8~12개 (한국어 중심, 필요시 짧은 영어도 허용)"]\n'
        "}\n"
        "규칙:\n"
        "- 반드시 JSON만 출력 (설명/마크다운 금지)\n"
        "- caption은 60자 이내 권장\n"
        "- tags는 중복 제거\n"
    )

    # 1) GCS URI 우선
    uri = _gcs_uri_for_key(key)
    image_part = None
    if uri:
        try:
            image_part = Part.from_uri(uri, mime_type=mime)
        except Exception:
            image_part = None

    # 2) URI 실패/불가면 bytes로
    if image_part is None:
        try:
            tmp_path, _mime = _download_storage_to_tmp(key)
            try:
                with open(tmp_path, "rb") as f:
                    data = f.read()
                image_part = Part.from_data(data=data, mime_type=mime)
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
        except Exception:
            return "", [], used_model

    try:
        mdl = GenerativeModel(used_model)
        resp = mdl.generate_content(
            [image_part, prompt],
            generation_config={"temperature": 0.2, "max_output_tokens": 512},
        )
        txt = getattr(resp, "text", "") or ""
        obj = _safe_json_extract_from_model(txt)

        caption = str(obj.get("caption") or "").strip()
        tags = obj.get("tags") or []

        if isinstance(tags, str):
            tags_list = re.split(r"[,\n]+", tags)
        elif isinstance(tags, list):
            tags_list = [str(x) for x in tags]
        else:
            tags_list = []

        tags_list = _uniq_keep_order([t.strip() for t in tags_list if t and str(t).strip()])
        if max_tags and len(tags_list) > max_tags:
            tags_list = tags_list[:max_tags]

        if not caption and not tags_list:
            return "", [], used_model

        return caption, tags_list, used_model

    except Exception:
        return "", [], used_model


# -----------------------------------------------------------------------------
# Core: add / update
# -----------------------------------------------------------------------------
def _tags_list_from_old_meta(old_meta: Dict[str, Any]) -> List[str]:
    """
    기존 메타에서 tags를 최대한 복원.
    (우선순위) tags_json -> tags_list(|구분) -> tags(공백 구분)
    """
    if not isinstance(old_meta, dict):
        return []

    tj = old_meta.get("tags_json")
    if isinstance(tj, str) and tj.strip():
        try:
            v = json.loads(tj)
            return _parse_tags(v)  # type: ignore[arg-type]
        except Exception:
            pass

    tl = old_meta.get("tags_list")
    if isinstance(tl, str) and tl.strip():
        parts = [p for p in tl.split("|") if p.strip()]
        return _parse_tags(parts)

    ts = old_meta.get("tags")
    if isinstance(ts, str) and ts.strip():
        parts = [p for p in ts.split() if p.strip()]
        return _parse_tags(parts)

    return []


def _get_existing_meta_by_pid(col: Any, pid: str) -> Dict[str, Any]:
    try:
        got = col.get(ids=[pid], include=["metadatas"])
        metas = got.get("metadatas") or []
        if metas and isinstance(metas[0], dict):
            return metas[0]
    except Exception:
        pass
    return {}


def add_image_item(
    *,
    path: str,
    embedding: List[float],
    caption: str = "",
    original_name: str = "",
    tags: Union[str, List[str], None] = None,
    extra_meta: Optional[Dict[str, Any]] = None,
    ai: Optional[bool] = None,
    ai_force: bool = False,
    ai_model: str = "",
) -> str:
    c = images_coll()

    key = (path or "").strip().replace("\\", "/").lstrip("/")
    if not key:
        key = f"images/unknown/{int(time.time())}.bin"

    size = _storage_size(key)
    mtime = _storage_mtime(key)
    pid = _pid_for_key(key)

    base_name = Path(key).name
    base_stem = Path(key).stem

    # ------------------------------------------------------------------
    # ✅ 기존 레코드가 있으면, 빈 입력(caption/tags/original_name)은 기존 값 유지
    # ------------------------------------------------------------------
    old_meta: Dict[str, Any] = {}
    try:
        got = c.get(ids=[pid], include=["metadatas"])
        metas = got.get("metadatas") or []
        if metas and isinstance(metas[0], dict):
            old_meta = metas[0]
    except Exception:
        old_meta = {}

    old_caption = (old_meta.get("caption") or "").strip() if isinstance(old_meta, dict) else ""
    old_orig = (old_meta.get("original_name") or "").strip() if isinstance(old_meta, dict) else ""
    old_tag_list = _tags_list_from_old_meta(old_meta)

    caption_in = (caption or "").strip()
    orig_in = (original_name or "").strip()

    caption_s = caption_in if caption_in else old_caption
    orig_s = orig_in if orig_in else old_orig

    # tags: None이면 미전달로 보고 기존 유지(기존이 없으면 [])
    if tags is None:
        tag_list = old_tag_list
    else:
        tag_list = _parse_tags(tags)
        # tags가 비어버리면(기본값/실수) 기존 유지
        if (not tag_list) and old_tag_list:
            tag_list = old_tag_list

    tags_str = " ".join(tag_list).strip()

    # ------------------------------------------------------------------
    # ✅ AI로 caption/tags 보강(필요 시)
    # ------------------------------------------------------------------
    ai_on = _ai_enabled(ai)
    model_name = (
        (ai_model or "").strip()
        or (os.getenv("IMAGE_META_MODEL") or "").strip()
        or (os.getenv("VERTEX_GEMINI_VISION_MODEL") or "").strip()
        or (os.getenv("VERTEX_GEMINI_MODEL") or "").strip()
        or "gemini-3.5-flash"
    )

    need_ai = bool(ai_on) and (ai_force or (not caption_s) or (not tags_str))
    if need_ai:
        try:
            mime = _guess_mime(key)
            ai_caption, ai_tags, used_model = _ai_caption_tags_for_storage_key(
                key=key, mime=mime, model_name=model_name
            )

            if isinstance(ai_caption, str) and ai_caption.strip():
                if ai_force or (not caption_s):
                    caption_s = ai_caption.strip()

            if ai_tags:
                merged = []
                if tag_list:
                    merged.extend(tag_list)
                merged.extend(ai_tags)
                tag_list = _parse_tags(merged)
                tags_str = " ".join(tag_list).strip()

            if used_model:
                model_name = used_model

        except Exception:
            log.exception("AI meta 생성 실패: %s", key)

    search_text = _build_search_text(caption_s, orig_s, tags_str, base_stem)

    # ✅ 기존 meta를 기반으로 update (기존 필드 보존)
    meta: Dict[str, Any] = dict(old_meta or {})
    meta.update(
        {
            "path": key,
            "mime": _guess_mime(key),
            "size": int(size),
            "mtime": int(mtime),
            "caption": caption_s,
            "original_name": orig_s,
            "basename": base_name,
            "tags": (tags_str or ""),
            "tags_count": int(len(tag_list)),
            "tags_list": "|".join(tag_list[:30]),
            "tags_json": json.dumps(tag_list[:30], ensure_ascii=False),
            "search_text": search_text,
        }
    )

    if need_ai:
        meta["ai_captioned"] = 1
        meta["ai_model"] = model_name

    if isinstance(extra_meta, dict):
        for k, v in extra_meta.items():
            if k in ("path",):
                continue
            meta[k] = _meta_scalar(v)

    doc_text = (search_text or caption_s or orig_s or base_name)[:2000]
    meta = {k: _meta_scalar(v) for k, v in (meta or {}).items()}

    # ------------------------------------------------------------------
    # ✅ upsert 우선(있으면) → 없으면 add/update로 최대한 안전하게
    # ------------------------------------------------------------------
    try:
        if hasattr(c, "upsert"):
            c.upsert(ids=[pid], embeddings=[embedding], documents=[doc_text], metadatas=[meta])
        else:
            try:
                c.add(ids=[pid], embeddings=[embedding], documents=[doc_text], metadatas=[meta])
            except Exception:
                if hasattr(c, "update"):
                    c.update(ids=[pid], metadatas=[meta], documents=[doc_text])
                else:
                    try:
                        c.delete(ids=[pid])
                    except Exception:
                        pass
                    c.add(ids=[pid], embeddings=[embedding], documents=[doc_text], metadatas=[meta])
    finally:
        _maybe_sync_up("add_image_item")

    return pid



def upsert_image_tags_caption(
    *,
    path: str,
    tags: Union[str, List[str], None] = None,
    caption: Optional[str] = None,          # ✅ None이면 "미전달"로 간주 → 기존 유지
    original_name: Optional[str] = None,    # ✅ None이면 기존 유지
    ai: Optional[bool] = None,
    ai_force: bool = False,
    ai_model: str = "",
) -> str:
    """
    기존 이미지(path)의 caption/tags/search_text/documents를 갱신.
    - embedding은 최대한 재사용
    - 없으면 스토리지에서 내려받아 재임베딩
    - ai 옵션을 켜면(또는 env) "기존 이미지에도 AI 메타 채우기"가 가능

    ✅ 중요 정책:
    - caption=None  : 캡션 미전달 → 기존 캡션 유지
    - caption=""    : 캡션을 의도적으로 비우기
    - tags=None     : 태그 미전달 → 기존 태그 유지
    - tags=[]/""    : 태그를 의도적으로 비우기(호출 측에서 명시)
    - original_name=None: 미전달 → 기존 유지
    """
    c = images_coll()

    key = (path or "").strip().replace("\\", "/").lstrip("/")
    if not key:
        raise ValueError("path is empty")

    pid = _pid_for_key(key)
    base_name = Path(key).name
    base_stem = Path(key).stem

    # ------------------------------------------------------------------
    # 기존 레코드 조회(임베딩 재사용) + 기존 meta 복원
    # ------------------------------------------------------------------
    old_meta: Dict[str, Any] = {}
    emb: Optional[List[float]] = None
    try:
        got = c.get(ids=[pid], include=["metadatas", "embeddings", "documents"])
        metas = got.get("metadatas") or []
        embs = got.get("embeddings") or []
        if metas and isinstance(metas[0], dict):
            old_meta = metas[0]
        if embs and isinstance(embs[0], list) and embs[0]:
            emb = embs[0]
    except Exception:
        pass

    # ✅ 기존 값(미전달 시 유지용)
    old_caption = (old_meta.get("caption") or "").strip() if isinstance(old_meta, dict) else ""
    old_orig = (old_meta.get("original_name") or "").strip() if isinstance(old_meta, dict) else ""
    old_tag_list = _tags_list_from_old_meta(old_meta)

    # ------------------------------------------------------------------
    # 입력값 결정: None이면 기존 유지, 값이 오면 그 값 사용(빈 문자열은 "비우기")
    # ------------------------------------------------------------------
    caption_s = old_caption if caption is None else (caption or "").strip()
    orig_s = old_orig if original_name is None else (original_name or "").strip()

    if tags is None:
        tag_list = old_tag_list
    else:
        tag_list = _parse_tags(tags)
    tags_str = " ".join(tag_list).strip()

    # ------------------------------------------------------------------
    # ✅ AI로 caption/tags 보강(필요 시)
    # ------------------------------------------------------------------
    ai_on = _ai_enabled(ai)
    model_name = (
        (ai_model or "").strip()
        or (os.getenv("IMAGE_META_MODEL") or "").strip()
        or (os.getenv("VERTEX_GEMINI_VISION_MODEL") or "").strip()
        or (os.getenv("VERTEX_GEMINI_MODEL") or "").strip()
        or "gemini-3.5-flash"
    )

    need_ai = bool(ai_on) and (ai_force or (not caption_s) or (not tags_str))
    if need_ai:
        try:
            mime = _guess_mime(key)
            ai_caption, ai_tags, used_model = _ai_caption_tags_for_storage_key(
                key=key, mime=mime, model_name=model_name
            )

            if isinstance(ai_caption, str) and ai_caption.strip():
                if ai_force or (not caption_s):
                    caption_s = ai_caption.strip()

            if ai_tags:
                merged: List[Any] = []
                if tag_list:
                    merged.extend(tag_list)
                merged.extend(ai_tags)
                tag_list = _parse_tags(merged)
                tags_str = " ".join(tag_list).strip()

            if used_model:
                model_name = used_model

        except Exception:
            log.exception("AI meta(upsert) 실패: %s", key)

    # ------------------------------------------------------------------
    # 임베딩이 없으면 재임베딩
    # ------------------------------------------------------------------
    if emb is None:
        tmp_path, mime = _download_storage_to_tmp(key)
        try:
            from ragapp.services.vertex_embed import embed_image_file
            emb = embed_image_file(tmp_path, mime=mime)
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    size = _storage_size(key)
    mtime = _storage_mtime(key)

    # ------------------------------------------------------------------
    # 메타 갱신(기존 meta 기반)
    # ------------------------------------------------------------------
    meta = dict(old_meta or {})
    meta.update(
        {
            "path": key,
            "mime": _guess_mime(key),
            "size": int(size),
            "mtime": int(mtime),
            "caption": caption_s,
            "original_name": orig_s,
            "basename": base_name,
            "tags": (tags_str or ""),
            "tags_count": int(len(tag_list)),
            "tags_list": "|".join(tag_list[:30]),
            "tags_json": json.dumps(tag_list[:30], ensure_ascii=False),
            "meta_updated_at": int(time.time()),
        }
    )

    search_text = _build_search_text(
        meta.get("caption", "") or "",
        meta.get("original_name", "") or "",
        meta.get("tags", "") or "",
        base_stem,
    )
    meta["search_text"] = search_text

    if need_ai:
        meta["ai_captioned"] = 1
        meta["ai_model"] = model_name

    doc_text = (search_text or caption_s or orig_s or base_name)[:2000]
    meta2 = {k: _meta_scalar(v) for k, v in (meta or {}).items()}

    # update/upsert 호환
    if hasattr(c, "update"):
        try:
            c.update(ids=[pid], metadatas=[meta2], documents=[doc_text])
            _maybe_sync_up_after_write("upsert_image_tags_caption(update)")
            return pid
        except Exception:
            pass

    if hasattr(c, "upsert"):
        try:
            c.upsert(ids=[pid], embeddings=[emb], metadatas=[meta2], documents=[doc_text])
            _maybe_sync_up_after_write("upsert_image_tags_caption(upsert)")
            return pid
        except Exception:
            pass

    try:
        c.delete(ids=[pid])
    except Exception:
        pass
    c.add(ids=[pid], embeddings=[emb], metadatas=[meta2], documents=[doc_text])
    _maybe_sync_up_after_write("upsert_image_tags_caption(add)")
    return pid


def upsert_image_tags_caption_by_pid(*, pid: str, caption: str = "", tags: str = "") -> dict:
    """
    (구버전/유틸) pid로 메타만 업데이트하고 싶을 때 사용.
    - embedding은 건드리지 않음
    """
    pid = str(pid or "").strip()
    if not pid:
        return {"ok": False, "error": "pid_required"}

    try:
        col = images_coll()
    except Exception as e:
        return {"ok": False, "error": "collection_unavailable", "detail": f"{e.__class__.__name__}: {e}"}

    try:
        got = col.get(ids=[pid], include=["metadatas", "documents"])
    except Exception as e:
        return {"ok": False, "error": "get_failed", "detail": str(e)}

    metas = got.get("metadatas") or []
    docs = got.get("documents") or []
    if not metas:
        return {"ok": False, "error": "not_found"}

    meta = metas[0] if isinstance(metas[0], dict) else {}
    doc0 = docs[0] if docs and isinstance(docs[0], str) else ""

    caption_s = (caption or "").strip()
    tags_s = (tags or "").strip()

    if caption_s:
        meta["caption"] = caption_s[:400]
    if tags_s:
        meta["tags"] = tags_s[:800]
        parts = [t.strip() for t in re.split(r"[,\s]+", tags_s) if t.strip()]
        meta["tags_count"] = int(len(parts))
        meta["tags_list"] = "|".join(parts[:30])
        meta["tags_json"] = json.dumps(parts[:30], ensure_ascii=False)

    caption_eff = str(meta.get("caption") or "")
    tags_eff = str(meta.get("tags") or "")
    orig_name = str(meta.get("orig_name") or meta.get("original_name") or "")
    search_text = " ".join([caption_eff, tags_eff, orig_name, doc0]).strip()[:1200]
    meta["search_text"] = _norm_text(search_text)
    meta["meta_updated_at"] = int(time.time())

    try:
        kwargs = {"ids": [pid], "metadatas": [meta]}
        if search_text:
            kwargs["documents"] = [search_text]
        col.update(**kwargs)
        _maybe_sync_up_after_write("upsert_image_tags_caption_by_pid")
        return {"ok": True, "pid": pid}
    except Exception as e:
        return {"ok": False, "error": "update_failed", "detail": str(e)}


# -----------------------------------------------------------------------------
# Query helpers
# -----------------------------------------------------------------------------
def search_images_by_text_embedding(*, text_embedding: List[float], k: int = 8) -> Dict[str, Any]:
    c = images_coll()
    return c.query(
        query_embeddings=[text_embedding],
        n_results=int(k),
        include=["metadatas", "documents", "distances"],
    )


# -----------------------------------------------------------------------------
# Tables
# -----------------------------------------------------------------------------
def add_table_rows(*, table_name: str, rows: List[Dict[str, Any]], embeddings: List[List[float]]) -> int:
    if len(rows) != len(embeddings):
        raise ValueError("rows와 embeddings 길이가 다릅니다.")

    c = table_coll()

    ids: List[str] = []
    docs: List[str] = []
    metas: List[Dict[str, Any]] = []

    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            row = {"value": row}

        pid = f"row:{table_name}:{i:08d}"
        doc = " | ".join(f"{k}:{row.get(k, '')}" for k in row.keys())

        try:
            row_json = json.dumps(row, ensure_ascii=False)
        except Exception:
            row_json = json.dumps({k: str(v) for k, v in row.items()}, ensure_ascii=False)

        ids.append(pid)
        docs.append(doc[:2000])
        metas.append({"table": str(table_name), "row_json": str(row_json)})

    B = 512
    for b in range(0, len(ids), B):
        c.add(
            ids=ids[b: b + B],
            embeddings=embeddings[b: b + B],
            documents=docs[b: b + B],
            metadatas=metas[b: b + B],
        )

    _maybe_sync_up_after_write("add_table_rows")
    return len(ids)


def search_table_by_text_embedding(*, text_embedding: List[float], k: int = 10):
    c = table_coll()
    return c.query(query_embeddings=[text_embedding], n_results=int(k))


def list_table_rows(*, k: int = 200) -> Dict[str, Any]:
    """Read table metadata without vector scoring for offline/local search."""
    c = table_coll()
    got = c.get(limit=max(1, int(k)), include=["metadatas", "documents"])
    metas = got.get("metadatas") or []
    docs = got.get("documents") or []
    return {
        "metadatas": [metas],
        "documents": [docs],
        "distances": [[None] * len(metas)],
    }
