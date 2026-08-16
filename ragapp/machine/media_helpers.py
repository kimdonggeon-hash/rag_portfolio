# ragapp/machine/media_helpers.py
from __future__ import annotations

import os
import json
import uuid
import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote, urlparse, parse_qs, unquote

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile

from ragapp.services.usage_limiter import check_and_increment_usage  # ✅ 하드 제한(429)

from .feature_config import MEDIA_URL

log = logging.getLogger(__name__)

from ragapp.services.gcs_signed_url import make_signed_url as make_gcs_signed_url

# ────────────────────────────────────────────────
# 공통 로그 (MyLog)
# ────────────────────────────────────────────────
def _log(
    request: HttpRequest, mode: str, query: str, ok: bool, extra: Dict[str, Any]
) -> None:
    """간단 서버 로그 + MyLog 테이블에 남기기."""
    try:
        from ragapp.models import MyLog  # lazy import(순환 최소화)

        ip = (
            request.META.get("REMOTE_ADDR", "")
            or request.META.get("HTTP_X_FORWARDED_FOR", "")
            or ""
        )
        MyLog.objects.create(
            mode_text=mode[:100],
            query=query[:500],
            ok_flag=ok,
            remote_addr_text=ip[:200],
            extra_json=extra,
        )
    except Exception:
        pass


# ────────────────────────────────────────────────
# Quota(429) 공통
# ────────────────────────────────────────────────
def _enforce_quota_or_429(
    request: HttpRequest,
    *,
    kind: str,
    template_name: str,
    base_ctx: Dict[str, Any],
    msg: str,
) -> Optional[HttpResponse]:
    """
    ✅ kind별 하루 사용량 하드 제한.
    - 허용이면 None 반환
    - 제한/오류면 HttpResponse(429/503) 반환
    """
    try:
        allowed, limit, used = check_and_increment_usage(request, kind)
    except Exception as e:
        log.exception("usage limiter(%s) 실패: %s", kind, e)
        ctx = dict(base_ctx)
        ctx["error"] = "사용량 체크 오류로 요청을 처리할 수 없습니다."
        return render(request, template_name, ctx, status=503)

    if not allowed:
        ctx = dict(base_ctx)
        ctx["error"] = msg
        ctx["limit"] = limit
        ctx["used"] = used
        ctx["kind"] = kind
        return render(request, template_name, ctx, status=429)

    return None


# ────────────────────────────────────────────────
# Storage JSON helpers
# ────────────────────────────────────────────────
PENDING_UPLOAD_META_PREFIX = "meta/pending_uploads"
APPROVED_UPLOAD_META_PREFIX = "meta/approved_uploads"
REJECTED_UPLOAD_META_PREFIX = "meta/rejected_uploads"
PUBLIC_IMAGE_ALLOWLIST_KEY = "meta/public_image_allowlist.json"
DELETED_IMAGE_IDS_KEY = "meta/deleted_image_ids.json"


def _read_json_from_storage(key: str) -> dict:
    try:
        if not default_storage.exists(key):
            return {}
        with default_storage.open(key, "rb") as f:
            raw = f.read()
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def _write_json_to_storage(key: str, payload: dict) -> None:
    try:
        if default_storage.exists(key):
            default_storage.delete(key)
        default_storage.save(
            key,
            ContentFile(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
        )
    except Exception as e:
        log.exception("write_json failed key=%s: %s", key, e)
        raise



# ────────────────────────────────────────────────
# allowlist (approve)
# ────────────────────────────────────────────────
def _normalize_storage_key(s: str) -> str:
    s = (s or "").strip().replace("\\", "/").lstrip("/")
    gs_loc = (getattr(settings, "GS_LOCATION", "") or "").strip().strip("/")
    if gs_loc and s.startswith(gs_loc + "/"):
        s = s[len(gs_loc) + 1 :]
    return s


def _load_public_allowlist() -> set[str]:
    data = _read_json_from_storage(PUBLIC_IMAGE_ALLOWLIST_KEY)
    if isinstance(data, list):
        return {str(x).strip() for x in data if str(x).strip()}
    if isinstance(data, dict):
        lst = data.get("keys") or data.get("allow") or data.get("allowlist") or []
        if isinstance(lst, list):
            return {str(x).strip() for x in lst if str(x).strip()}
    return set()


def _save_public_allowlist(keys: set[str]) -> None:
    _write_json_to_storage(PUBLIC_IMAGE_ALLOWLIST_KEY, sorted(keys))


def _approve_public_storage_key(storage_key: str) -> None:
    k = _normalize_storage_key(storage_key or "")
    if not k:
        return
    keys = _load_public_allowlist()
    keys.add(k)
    _save_public_allowlist(keys)


def _list_meta_ids(prefix: str, limit: int = 200) -> list[str]:
    prefix = prefix.rstrip("/") + "/"
    try:
        _, files = default_storage.listdir(prefix)

        out: list[str] = []
        for fn in files:
            if not fn.endswith(".json"):
                continue
            pid = Path(fn).stem
            if pid:
                out.append(pid)
            if len(out) >= limit:
                break

        # ✅ 최신이 위로 오게(선택)
        out.sort(reverse=True)
        return out

    except FileNotFoundError:
        # An empty queue is the normal state on a fresh local installation.
        # FileSystemStorage does not create list-only directories in advance.
        return []
    except Exception as e:
        # ✅ 여기 로그가 Cloud Run 로그에 찍히면 99% 원인 바로 나옴
        log.exception("list_meta_ids failed (prefix=%s): %s", prefix, e)
        return []


def _list_pending_ids(limit: int = 200) -> list[str]:
    return _list_meta_ids(PENDING_UPLOAD_META_PREFIX, limit=limit)


def _list_rejected_ids(limit: int = 200) -> list[str]:
    return _list_meta_ids(REJECTED_UPLOAD_META_PREFIX, limit=limit)


def _list_approved_ids(limit: int = 200) -> list[str]:
    return _list_meta_ids(APPROVED_UPLOAD_META_PREFIX, limit=limit)


def _promote_pending_object(*, src_key: str, dest_prefix: str) -> tuple[str, bytes]:
    """
    pending/... -> images/... 로 복사 후 pending 삭제(가능하면)
    반환: (dest_key, bytes)
    """
    src = _normalize_storage_key(src_key or "")
    if not src:
        raise RuntimeError("invalid_src_key")

    if not src.startswith("pending/"):
        raise RuntimeError("src_not_pending")

    name = Path(src).name
    if not name:
        raise RuntimeError("invalid_src_name")

    dst = f"{dest_prefix.rstrip('/')}/{name}"
    dst = _normalize_storage_key(dst)

    if default_storage.exists(dst):
        stem = Path(name).stem
        suf = Path(name).suffix
        dst = _normalize_storage_key(
            f"{dest_prefix.rstrip('/')}/{stem}_{uuid.uuid4().hex[:8]}{suf}"
        )

    with default_storage.open(src, "rb") as rf:
        data = rf.read()

    default_storage.save(dst, ContentFile(data))

    try:
        if default_storage.exists(src):
            default_storage.delete(src)
    except Exception:
        pass

    return dst, data


# ────────────────────────────────────────────────
# deleted ids (soft delete)
# ────────────────────────────────────────────────
def _load_deleted_image_ids() -> set[str]:
    try:
        if not default_storage.exists(DELETED_IMAGE_IDS_KEY):
            return set()
        with default_storage.open(DELETED_IMAGE_IDS_KEY, "rb") as f:
            raw = f.read()
        data = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw))
        if isinstance(data, list):
            return {str(x) for x in data}
        return set()
    except Exception:
        return set()


def _save_deleted_image_ids(ids: set[str]) -> None:
    try:
        try:
            if default_storage.exists(DELETED_IMAGE_IDS_KEY):
                default_storage.delete(DELETED_IMAGE_IDS_KEY)
        except Exception:
            pass
        payload = json.dumps(sorted(ids), ensure_ascii=False)
        default_storage.save(DELETED_IMAGE_IDS_KEY, ContentFile(payload.encode("utf-8")))
    except Exception:
        pass


def _delete_image_from_index(pid: str) -> None:
    ids = _load_deleted_image_ids()
    ids.add(str(pid))
    _save_deleted_image_ids(ids)


# ────────────────────────────────────────────────
# path/key resolve helpers
# ────────────────────────────────────────────────
def _norm_rel_key(s: str) -> str:
    s = _normalize_storage_key((s or "").strip())
    s = s.replace("\\", "/").lstrip("/")
    if not s:
        return ""
    if ".." in Path(s).parts:
        return ""
    return s


def _pick_raw_path(meta: dict, doc: str = "") -> str:
    """
    예전 데이터 호환:
    - meta에 path/filepath가 없고 url/storage_key/key 등에만 있는 경우를 커버
    - URL이면 querystring(path=..., key=...)도 추출
    - doc 문자열 안에 images/... 경로가 박혀있는 경우도 약하게 추출
    """
    cand: list[str] = []

    def _add(v: object):
        if isinstance(v, str):
            s = v.strip()
            if s and s.lower() not in ("-", "none", "null"):
                cand.append(s)

    for k in ("path", "filepath", "storage_key", "key", "object", "name", "filename"):
        _add(meta.get(k))

    for k in ("url", "file_url", "media_url", "download_url"):
        v = meta.get(k)
        if isinstance(v, str) and v.strip():
            s = v.strip()
            cand.append(s)
            try:
                u = urlparse(s)
                qs = parse_qs(u.query or "")
                for qk in ("path", "key", "object", "name", "filename"):
                    if qk in qs and qs[qk]:
                        cand.append(qs[qk][0])
                        cand.append(unquote(qs[qk][0]))
            except Exception:
                pass

    if isinstance(doc, str) and doc:
        import re as _re
        m = _re.search(
            r"(images\/\d{4}\/\d{2}\/[0-9A-Za-z_\-\.%]+\.(?:jpg|jpeg|png|webp|gif))",
            doc,
            flags=_re.I,
        )
        if m:
            cand.append(m.group(1))
            cand.append(unquote(m.group(1)))

    return cand[0] if cand else ""


def _resolve_existing_storage_key(raw: str) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None

    low = s.lower()
    if low in ("-", "none", "(비공개 경로)"):
        return None

    extracted: list[str] = []

    def _add(x: str | None) -> None:
        x = (x or "").strip()
        if x:
            extracted.append(x)

    _add(s)

    if s.startswith(("http://", "https://", "file://")):
        try:
            u = urlparse(s)
            try:
                qs = parse_qs(u.query or "")
                for key in ("path", "key", "object", "name", "filename", "file", "storage_key", "media"):
                    if key in qs and qs[key]:
                        _add(unquote(qs[key][0]))
            except Exception:
                pass

            path = (u.path or "").strip()
            if path:
                _add(path)
                _add(path.lstrip("/"))
                parts = [p for p in path.split("/") if p]
                if parts:
                    _add("/".join(parts))
                    if len(parts) >= 2:
                        _add("/".join(parts[1:]))

                    for i, p in enumerate(parts):
                        if p in ("uploads", "media", "images"):
                            _add("/".join(parts[i:]))
                            break

            try:
                parts = [p for p in (u.path or "").split("/") if p]
                if "b" in parts and "o" in parts:
                    bi = parts.index("b")
                    oi = parts.index("o")
                    if bi + 1 < len(parts) and oi + 1 < len(parts):
                        obj = "/".join(parts[oi + 1 :])
                        _add(unquote(obj))
            except Exception:
                pass

            try:
                host = (u.netloc or "").split("@")[-1].split(":")[0].lower()
                if host.endswith(".storage.googleapis.com"):
                    _add((u.path or "").lstrip("/"))
                if host == "storage.googleapis.com":
                    parts = [p for p in (u.path or "").split("/") if p]
                    if len(parts) >= 2:
                        _add("/".join(parts[1:]))
            except Exception:
                pass

        except Exception:
            pass

    if s.startswith("gs://"):
        try:
            t = s[5:]
            t = t.split("?", 1)[0].split("#", 1)[0]
            if "/" in t:
                _bucket, obj = t.split("/", 1)
                _add(obj)
        except Exception:
            pass

    # MEDIA_URL prefix 제거 후보
    try:
        base = (MEDIA_URL or "/uploads/").strip()
        base = "/" + base.strip("/") + "/"
        for cand in list(extracted):
            c = (cand or "").strip()
            if c.startswith(base):
                _add(c[len(base):])
    except Exception:
        pass

    seen: set[str] = set()

    for cand in extracted:
        k0 = _norm_rel_key(cand)
        if not k0 or k0 in seen:
            continue
        seen.add(k0)

        variants: list[str] = [k0]

        if k0.startswith("uploads/"):
            variants.append(k0[len("uploads/"):])
        else:
            variants.append("uploads/" + k0)

        if k0.startswith("media/"):
            variants.append(k0[len("media/"):])
            variants.append("uploads/" + k0[len("media/"):])
        else:
            variants.append("media/" + k0)

        for v in variants:
            vv = _norm_rel_key(v)
            if not vv:
                continue
            try:
                if default_storage.exists(vv):
                    return vv
            except Exception:
                continue

    return None


def _build_upload_url_from_key(real_key: str) -> str:
    base = (MEDIA_URL or "/uploads/").rstrip("/")
    k = _norm_rel_key(real_key)

    if k.startswith("uploads/"):
        k = k[len("uploads/"):]
    return f"{base}/{quote(k, safe='/')}"


# ────────────────────────────────────────────────
# Signed URL helpers (Cloud Run keyless)
# ────────────────────────────────────────────────
@lru_cache(maxsize=1)
def _signing_ctx():
    import google.auth
    from google.auth.transport.requests import Request

    creds, _ = google.auth.default()
    req = Request()
    return creds, req


def _get_access_token() -> str:
    creds, req = _signing_ctx()
    exp = getattr(creds, "expiry", None)
    now = datetime.now(dt_timezone.utc)

    if (not getattr(creds, "token", None)) or (exp and exp <= now + timedelta(minutes=2)):
        creds.refresh(req)
    return str(getattr(creds, "token", "") or "")


def _signed_url_for_storage_key(storage_key: str, expires_sec: int = 1800) -> str:
    try:
        key = (storage_key or "").strip().replace("\\", "/").lstrip("/")
        if not key or key.lower() in ("-", "none"):
            return ""

        # (유지) GS_LOCATION이 설정된 경우 object_name에 prefix 붙이기
        gs_loc = (getattr(settings, "GS_LOCATION", "") or "").strip().strip("/")
        object_name = key
        if gs_loc and not key.startswith(gs_loc + "/"):
            object_name = f"{gs_loc}/{key}"

        # ✅ 여기서 바로 IAM Signer 기반 Signed URL 생성
        return make_gcs_signed_url(object_name, expires_sec=int(expires_sec)) or ""
    except Exception:
        return ""


def _safe_media_url(storage_key: str) -> Optional[str]:
    try:
        key = (storage_key or "").strip().replace("\\", "/").lstrip("/")
        if not key:
            return None
        if key.startswith(("http://", "https://")):
            return None
        low = key.lower()
        if low in ("-", "none", "null"):
            return None

        key = _normalize_storage_key(key)
        if not key:
            return None

        signed = _signed_url_for_storage_key(key, expires_sec=1800)
        if signed:
            return signed

        key2 = key
        if key2.startswith("uploads/"):
            key2 = key2[len("uploads/"):]

        base = (getattr(settings, "MEDIA_URL", "") or "/uploads/").rstrip("/")
        return f"{base}/{quote(key2, safe='/')}"
    except Exception:
        return None
