# ragapp/media_views.py
from __future__ import annotations

import mimetypes
import os
import logging
from pathlib import Path
from urllib.parse import unquote

from django.apps import apps
from django.conf import settings
from django.core.files.storage import default_storage
from django.http import (
    FileResponse,
    Http404,
    HttpResponse,
    JsonResponse,
    HttpResponseRedirect,
)
from django.views.decorators.http import require_GET, require_http_methods
from django.contrib.admin.views.decorators import staff_member_required

from ragapp.machine.media_helpers import (
    PENDING_UPLOAD_META_PREFIX,
    _read_json_from_storage,
)

# ✅ IAM Signer 기반 Signed URL 생성(키 파일 없이 Cloud Run에서 동작)
from ragapp.services.gcs_signed_url import make_signed_url as make_gcs_signed_url

log = logging.getLogger("ragapp.media_views")


# =============================================================================
# small utils
# =============================================================================

def _truthy(v: str | None) -> bool:
    s = (v or "").strip().lower()
    return s in ("1", "true", "yes", "y", "on")


def _use_signed_url() -> bool:
    """
    - UPLOADS_USE_SIGNED_URL 가 명시되면 그 값을 따름
    - 없으면 DEBUG=False(운영)일 때만 Signed URL 사용
    """
    v = os.environ.get("UPLOADS_USE_SIGNED_URL")
    if v is not None:
        return _truthy(v)
    return not bool(getattr(settings, "DEBUG", False))


def _norm_key(s: str) -> str:
    """
    default_storage 상의 상대 키로 정규화.
    - URL 인코딩 해제
    - 윈도우 슬래시 -> POSIX
    - 양쪽 슬래시 제거(끝에 / 붙는 케이스 방지)
    - path traversal 방지
    """
    s = unquote((s or "").strip()).replace("\\", "/").strip("/")
    if not s:
        return ""
    if ".." in Path(s).parts:
        return ""
    return s


# =============================================================================
# gcsfuse helpers  (mount: /mnt/gcs)
# =============================================================================

_GCSFUSE_ROOT = Path("/mnt/gcs")


def _safe_join(root: Path, rel: str) -> Path | None:
    nk = _norm_key(rel)
    if not nk:
        return None
    try:
        cand = (root / nk).resolve()
        root_r = root.resolve()
        cand.relative_to(root_r)  # path traversal 방지
        return cand
    except Exception:
        return None


def _pick_gcsfuse_path(object_name: str) -> Path | None:
    """
    object_name (예: pending/images/2026/02/x.jpg)을
    gcsfuse 로컬 경로(/mnt/gcs/...)로 매핑해서 존재하면 반환.

    ✅ 사용자 환경: /mnt/gcs 가 바로 버킷 루트로 마운트된 형태라고 가정.
    """
    try:
        if not (_GCSFUSE_ROOT.exists() and _GCSFUSE_ROOT.is_dir()):
            return None
    except Exception:
        return None

    p = _safe_join(_GCSFUSE_ROOT, object_name)
    if p and p.exists() and p.is_file():
        return p
    return None


def _file_resp_from_path(p: Path, ctype: str, cache: str = "private, max-age=60") -> FileResponse:
    # FileResponse는 file-like object를 받으므로 open()을 전달
    resp = FileResponse(open(p, "rb"), content_type=ctype)
    resp["Cache-Control"] = cache
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


# =============================================================================
# uploads key helpers (public /uploads/* 지원용)
# =============================================================================

def _candidates(key: str) -> list[str]:
    key = _norm_key(key)
    if not key:
        return []

    cands = [key]

    # uploads/ prefix 유무 혼재 대응
    if key.startswith("uploads/"):
        cands.append(key[len("uploads/"):])
    else:
        cands.append("uploads/" + key)

    # media/는 "요청이 media/로 시작한 경우"에만 후보에 넣는다 (오탐 방지)
    if key.startswith("media/"):
        rest = key[len("media/"):]
        cands.append(rest)
        cands.append("uploads/" + rest)  # media/ -> uploads/ 로도 시도

    out: list[str] = []
    seen: set[str] = set()
    for x in cands:
        x = _norm_key(x)
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _pick_real_key(key: str) -> str | None:
    for cand in _candidates(key):
        try:
            if default_storage.exists(cand):
                return cand
        except Exception:
            continue
    return None


def _build_gcs_object_name(key: str) -> str:
    """
    요청 key(/uploads/<key>)를 GCS object name으로 변환.
    기본 정책:
    - /uploads/images/...  -> object: uploads/images/...
    - /uploads/uploads/... -> object: uploads/...
    - /uploads/media/...   -> object: uploads/<media 뒤 경로>
    """
    nk = _norm_key(key)
    if not nk:
        return ""

    if nk.startswith("uploads/"):
        return nk

    if nk.startswith("media/"):
        return "uploads/" + nk[len("media/"):]

    return "uploads/" + nk


def _signed_url_for_object(object_name: str) -> str:
    """
    ✅ Cloud Run(토큰 기반 ADC)에서도 동작하도록
    ragapp/services/gcs_signed_url.py의 IAM Signer 경로로 Signed URL을 생성한다.
    """
    exp_min = int(os.environ.get("SIGNED_URL_EXPIRE_MIN", "15"))
    url = make_gcs_signed_url(object_name, expires_sec=exp_min * 60)
    if not url:
        raise RuntimeError("signed url empty")
    return url


# =============================================================================
# Pending model fallback helpers (optional)
# =============================================================================

def _get_pending_model():
    """
    settings.MEDIA_PENDING_MODEL = "app_label.ModelName"
    기본값: ragapp.PendingMediaItem
    """
    label = getattr(settings, "MEDIA_PENDING_MODEL", "ragapp.PendingMediaItem")
    try:
        app_label, model_name = label.split(".", 1)
    except ValueError:
        log.error("MEDIA_PENDING_MODEL must be 'app_label.ModelName' but got: %r", label)
        return None
    try:
        return apps.get_model(app_label, model_name)
    except Exception as e:
        log.error("pending model load failed: %r (%s)", label, e)
        return None


def _pending_object_name_from_db_key(db_key: str) -> str:
    """
    Pending 검수용 DB key -> object name
    - DB에 'pending/...' 형태로 저장되어 있다는 전제
    - 안전을 위해 pending/ prefix 아니면 거절
    """
    nk = _norm_key(db_key)
    if not nk:
        return ""
    if not nk.startswith("pending/"):
        return ""
    return nk


# =============================================================================
# uploads probe/proxy
# =============================================================================

@require_GET
def uploads_probe(request):
    """
    진단용:
    /uploads/probe?key=images/....
    - 현재 런타임 default_storage 클래스
    - 후보 키 목록과 exists 결과를 JSON으로 반환
    """
    key = request.GET.get("key", "") or ""
    cands = _candidates(key)

    exists_map: dict[str, object] = {}
    for c in cands:
        try:
            exists_map[c] = bool(default_storage.exists(c))
        except Exception as e:
            exists_map[c] = f"ERROR: {type(e).__name__}: {e}"

    storage_cls = default_storage.__class__
    return JsonResponse(
        {
            "ok": True,
            "storage_class": f"{storage_cls.__module__}.{storage_cls.__name__}",
            "key": key,
            "candidates": cands,
            "exists": exists_map,
            "signed_url_mode": _use_signed_url(),
            "gcs_object_name_example": _build_gcs_object_name(key),
            "gcsfuse_root": str(_GCSFUSE_ROOT),
        },
        json_dumps_params={"ensure_ascii": False},
    )


@require_http_methods(["GET", "HEAD"])
def uploads_proxy(request, key: str):
    """
    /uploads/<path:key>

    운영(기본): Signed URL(302)로 리다이렉트  ✅ 권장
    - Cloud Run에서 /mnt/gcs(gcsfuse)로 직접 FileResponse 하는 대신,
      브라우저가 GCS Signed URL로 직접 가져가게 해서 안정화.

    로컬/디버그 또는 Signed URL 실패 시: default_storage 스트리밍으로 폴백.

    ✅ staff + ?raw=1 이면 Signed URL을 강제로 스킵하고 스트리밍으로 내려준다.
    """
    if key == "__ping__":
        return HttpResponse("uploads_proxy OK", content_type="text/plain")

    if not key or key.startswith(("/", "\\")):
        raise Http404("bad path")

    force_raw = bool(
        getattr(request, "user", None)
        and getattr(request.user, "is_staff", False)
        and request.GET.get("raw") == "1"
    )

    # 1) Signed URL (운영 기본)
    if _use_signed_url() and (not force_raw):
        object_name = _build_gcs_object_name(key)
        if not object_name:
            raise Http404("bad path")
        try:
            url = _signed_url_for_object(object_name)
            resp = HttpResponseRedirect(url)
            resp["Cache-Control"] = "private, max-age=60"
            return resp
        except Exception as e:
            log.warning("signed url failed: %s (%s)", type(e).__name__, e)

    # 2) 폴백: default_storage에서 직접 내려주기
    real_key = _pick_real_key(key)
    if not real_key:
        raise Http404("not found")

    ctype, _ = mimetypes.guess_type(real_key)
    ctype = ctype or "application/octet-stream"

    if request.method == "HEAD":
        resp = HttpResponse(content_type=ctype)
        resp["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp

    try:
        f = default_storage.open(real_key, "rb")
    except Exception:
        raise Http404("not found")

    resp = FileResponse(f, content_type=ctype)
    resp["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


# =============================================================================
# pending raw preview proxy (staff only)  ✅ gcsfuse(/mnt/gcs) 우선
# =============================================================================

@require_http_methods(["GET", "HEAD"])
@staff_member_required
def pending_raw_proxy(request, item_id: str):
    """
    /ragadmin/media/pending/raw/<pending_id> (staff only)

    1) meta/pending_uploads/<pending_id>.json 기반으로 storage_key 찾기 (media_machine 흐름)
    2) fallback: DB pending 모델 (있을 때만)
    3) ✅ gcsfuse 로컬 파일(/mnt/gcs/<object_name>) 우선 스트리밍
       - 없으면 default_storage.open 으로 폴백
       - (옵션) 운영에서는 signed redirect도 가능하지만, 여기서는 "같은 오리진" 안정성을 우선
    """
    force_raw = bool(
        getattr(request, "user", None)
        and getattr(request.user, "is_staff", False)
        and request.GET.get("raw") == "1"
    )

    def _serve_object(object_name: str, ctype: str) -> HttpResponse:
        # 0) (선택) signed redirect: 운영에서만, raw=1이면 스킵
        #    - redirect=1 일 때만 signed redirect를 사용
        if _use_signed_url() and (not force_raw) and request.GET.get("redirect") == "1":
            try:
                url = _signed_url_for_object(object_name)
                resp = HttpResponseRedirect(url)
                resp["Cache-Control"] = "private, max-age=60"
                return resp
            except Exception as e:
                log.warning("pending signed url failed: %s (%s)", type(e).__name__, e)

        # 1) ✅ gcsfuse 로컬 파일 우선
        lp = _pick_gcsfuse_path(object_name)
        if lp is not None:
            if request.method == "HEAD":
                resp = HttpResponse(content_type=ctype)
                resp["Cache-Control"] = "private, max-age=60"
                resp["X-Content-Type-Options"] = "nosniff"
                try:
                    resp["Content-Length"] = str(lp.stat().st_size)
                except Exception:
                    pass
                resp["X-RAG-Source"] = "gcsfuse"
                return resp

            resp = _file_resp_from_path(lp, ctype, cache="private, max-age=60")
            resp["X-RAG-Source"] = "gcsfuse"
            return resp

        # 2) fallback: default_storage (로컬/테스트/마운트 이슈 대비)
        if request.method == "HEAD":
            resp = HttpResponse(content_type=ctype)
            resp["Cache-Control"] = "private, max-age=60"
            resp["X-Content-Type-Options"] = "nosniff"
            resp["X-RAG-Source"] = "storage"
            return resp

        try:
            f = default_storage.open(object_name, "rb")
        except Exception:
            raise Http404("not found")

        resp = FileResponse(f, content_type=ctype)
        resp["Cache-Control"] = "private, max-age=60"
        resp["X-Content-Type-Options"] = "nosniff"
        resp["X-RAG-Source"] = "storage"
        return resp

    # ------------------------------------------------------------------
    # 1) ✅ meta JSON 기반 (media_machine pending 흐름)
    # ------------------------------------------------------------------
    meta_key = f"{PENDING_UPLOAD_META_PREFIX}/{item_id}.json"
    meta = _read_json_from_storage(meta_key) or {}
    storage_key = _norm_key(str(meta.get("storage_key") or "").strip())

    if storage_key:
        object_name = storage_key  # 보통 pending/... 형태
        ctype = str(meta.get("mime") or "").strip() or mimetypes.guess_type(object_name)[0] or "image/jpeg"
        return _serve_object(object_name, ctype)

    # ------------------------------------------------------------------
    # 2) fallback: DB pending 모델 기반(있을 때만)
    # ------------------------------------------------------------------
    Model = _get_pending_model()
    if Model is None:
        raise Http404("pending meta not found")

    try:
        item = Model.objects.get(pk=item_id)
    except Exception:
        raise Http404("not found")

    object_name = _pending_object_name_from_db_key(getattr(item, "key", "") or "")
    if not object_name:
        raise Http404("bad key")

    ctype = getattr(item, "mime", None) or mimetypes.guess_type(object_name)[0] or "image/jpeg"
    return _serve_object(object_name, ctype)