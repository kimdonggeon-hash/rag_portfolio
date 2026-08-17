# ragapp/machine/media_machine.py
from __future__ import annotations

import os
import re
import mimetypes
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import logging
from django.urls import reverse
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render, redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_protect
from django.contrib.admin.views.decorators import staff_member_required
from django.utils import timezone
from django.core.files.storage import default_storage
from django.core.files.base import File, ContentFile

from ragapp.services.image_moderation import is_public_image, get_actor_key

from .feature_config import (
    PUBLIC_ALLOW_UPLOAD_IMAGES,
    PUBLIC_MAX_FILES,
    PUBLIC_MAX_FILE_MB,
    _int,
    _tmp_upload_dir,
)
from .media_helpers import (
    _log,
    _enforce_quota_or_429,
    _normalize_storage_key,
    _safe_media_url,
    _signed_url_for_storage_key,
    _load_deleted_image_ids,
    _delete_image_from_index,
    _pick_raw_path,
    _resolve_existing_storage_key,
    _read_json_from_storage,
    _write_json_to_storage,
    _list_pending_ids,
    _list_rejected_ids,
    _list_approved_ids,
    _promote_pending_object,
    _approve_public_storage_key,
    PENDING_UPLOAD_META_PREFIX,
    APPROVED_UPLOAD_META_PREFIX,
    REJECTED_UPLOAD_META_PREFIX,
)
from .media_ai import (
    PUBLIC_IMAGE_UPLOAD_AI,
    IMAGE_AI_MAX_TAGS,
    _merge_tags,
    ai_caption_tags_from_image,
)

log = logging.getLogger(__name__)


def _pending_preview_url(req: HttpRequest, pending_id: str) -> str:
    """
    pending 미리보기는 무조건 같은 오리진 경로로 통일
    - urls.py에 name="ragadmin_media_pending_raw" 필요
    - raw=1: pending_raw_proxy에서 signed redirect 스킵/스트리밍 강제용
    """
    rel = reverse("ragadmin_media_pending_raw", kwargs={"item_id": pending_id})
    return req.build_absolute_uri(rel) + "?raw=1"


def _rejected_preview_url(req: HttpRequest, rejected_id: str) -> str:
    rel = reverse("ragadmin_media_rejected_raw", kwargs={"item_id": rejected_id})
    return req.build_absolute_uri(rel) + "?raw=1"


def _approved_preview_url(req: HttpRequest, storage_key: str) -> str:
    # 승인된 이미지는 이미 공개 허용목록에 등록돼 있어서, pending/rejected와
    # 달리 별도 staff 전용 raw 프록시 없이 일반 업로드 서빙 경로를 그대로 쓴다.
    rel = reverse("uploads_proxy", kwargs={"key": storage_key})
    return req.build_absolute_uri(rel)


# ────────────────────────────────────────────────
# Vertex 임베딩/LLM 헬퍼
# ────────────────────────────────────────────────
try:
    from ragapp.services.vertex_embed import (
        embed_image_file,
        embed_text_mm,
        embed_texts_vertex as embed_texts,
        infer_table_query_with_vertex,  # keep compat
    )
except Exception:  # pragma: no cover
    from ragapp.services.vertex_embed import (
        embed_image_file,
        embed_text_mm,
        embed_texts_vertex as embed_texts,
    )

    infer_table_query_with_vertex = None  # type: ignore


# ────────────────────────────────────────────────
# Chroma (images)
# ────────────────────────────────────────────────
CHROMA_MEDIA_IMPORT_ERR = None
try:
    from ragapp.services import chroma_media as cm

    add_image_item = cm.add_image_item
    search_images_by_text_embedding = cm.search_images_by_text_embedding
    images_space = getattr(cm, "images_space", lambda: "cosine")
except Exception as e:  # pragma: no cover
    CHROMA_MEDIA_IMPORT_ERR = f"{e.__class__.__name__}: {e}"
    log.exception("chroma_media import failed: %s", CHROMA_MEDIA_IMPORT_ERR)
    add_image_item = None  # type: ignore
    search_images_by_text_embedding = None  # type: ignore

    def images_space() -> str:  # type: ignore
        return "cosine"


def _safe_actor_key(req: HttpRequest) -> str:
    try:
        return str(get_actor_key(req) or "").strip()
    except Exception:
        return ""


def _is_safe_ext(ext: str) -> bool:
    if not ext or not ext.startswith("."):
        return False
    body = ext[1:]
    return body.isalnum() and (1 <= len(body) <= 6)


def _guess_ext(orig_name: str, content_type: str) -> str:
    orig_ext = (Path(orig_name).suffix or "").lower()
    if _is_safe_ext(orig_ext):
        return orig_ext
    guessed = mimetypes.guess_extension(content_type) if content_type else None
    if guessed and _is_safe_ext(guessed):
        return guessed
    return ".jpg"


# ────────────────────────────────────────────────
# (A) Public: 이미지 인덱싱 (업로드는 pending 저장)
# ────────────────────────────────────────────────
@never_cache
@require_http_methods(["GET", "POST"])
def media_index_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        # ✅ 업로드 직후 /media/index/?pending=... 로 돌아온 경우: "내 접수 확인" 카드 생성
        pending_param = (request.GET.get("pending") or "").strip()
        pending_cards: list[dict[str, Any]] = []
        pending_ids: list[str] = []

        if pending_param:
            # pid 안전 필터(32자리 hex만)
            for raw in pending_param.split(","):
                pid = (raw or "").strip().lower()
                if re.fullmatch(r"[0-9a-f]{32}", pid):
                    pending_ids.append(pid)
            pending_ids = pending_ids[:50]

            actor_key = _safe_actor_key(request)

            for pid in pending_ids:
                meta_key = f"{PENDING_UPLOAD_META_PREFIX}/{pid}.json"
                meta = _read_json_from_storage(meta_key) or {}
                meta_actor = str(meta.get("actor_key") or "").strip()

                # ✅ 내 업로드만 보여주기(보안)
                # actor_key가 비어있거나 meta_actor가 없으면 보여주지 않음(보수적으로)
                if not actor_key or not meta_actor or (meta_actor != actor_key):
                    continue

                pending_cards.append(
                    {
                        "status": "PENDING",
                        "msg": "업로드 접수 완료(검수 대기)",
                        "pid": pid,
                        "url": None,  # public에선 url 노출 X
                        "mime": str(meta.get("mime") or ""),
                        "size": int(meta.get("size") or 0),
                        "sha16": str(meta.get("sha16") or ""),
                        "actor_key": meta_actor,
                        "storage_key": "",  # public 노출 방지
                        "indexed_ok": False,
                        "chroma_col": None,
                        "chroma_count": None,
                        "caption": str(meta.get("caption") or ""),
                        "tags": str(meta.get("tags") or ""),
                        "ai_used": bool(meta.get("ai_used")),
                        "ai_model": str(meta.get("ai_model") or ""),
                        "ai_error": str(meta.get("ai_error") or ""),
                    }
                )

        ctx = {
            "allow_upload": PUBLIC_ALLOW_UPLOAD_IMAGES,
            "max_files": PUBLIC_MAX_FILES,
            "max_file_mb": PUBLIC_MAX_FILE_MB,
            "ai_on_upload": PUBLIC_IMAGE_UPLOAD_AI,
        }

        # ✅ pending으로 들어온 경우: 같은 media_index.html에서 "확인 화면"처럼 보이게 cards 전달
        if pending_param:
            ctx.update(
                {
                    "cards": pending_cards,
                    "ok": len(pending_cards),
                    "fail": 0,
                    "pending_ids": pending_ids,
                    "pending_view": True,  # (선택) 템플릿에서 분기용
                    "error": "" if pending_cards else "접수 항목을 찾지 못했습니다(이미 검수되었거나 세션이 달라졌을 수 있어요).",
                }
            )

        return render(request, "ragapp/media_index.html", ctx)


    if not PUBLIC_ALLOW_UPLOAD_IMAGES:
        return render(
            request,
            "ragapp/media_index.html",
            {"allow_upload": False, "error": "이미지 업로드가 비활성화되었습니다."},
        )

    # 승인 단계에서 chroma 인덱싱을 하기 때문에, 여기서도 모듈 체크는 유지
    if add_image_item is None:
        msg = "이미지 인덱싱 모듈을 불러올 수 없습니다."
        if getattr(request, "user", None) and request.user.is_staff and CHROMA_MEDIA_IMPORT_ERR:
            msg = f"이미지 인덱싱 모듈 import 실패: {CHROMA_MEDIA_IMPORT_ERR}"
        return render(
            request,
            "ragapp/media_index.html",
            {"allow_upload": PUBLIC_ALLOW_UPLOAD_IMAGES, "error": msg},
            status=500,
        )

    files = request.FILES.getlist("images")[:PUBLIC_MAX_FILES]
    use_caption_from_name = bool(request.POST.get("caption_from_name"))
    common_caption = (request.POST.get("caption") or "").strip()
    common_tags = (request.POST.get("tags") or "").strip()

    actor_key = _safe_actor_key(request)

    cards: List[Dict[str, Any]] = []
    ok, fail = 0, 0
    tmp_dir = _tmp_upload_dir()

    for f in files:
        status = "PENDING"
        msg = ""
        pid = ""
        url: Optional[str] = None
        mime = ""
        sha16 = ""
        storage_key = ""
        indexed_ok = False
        chroma_col = None
        chroma_count = None

        caption_final = ""
        tags_final = ""
        ai_used = False
        ai_model = ""
        ai_error = ""

        size_bytes = int(getattr(f, "size", 0) or 0)
        tmp_path: Optional[Path] = None

        try:
            if size_bytes > PUBLIC_MAX_FILE_MB * 1024 * 1024:
                raise RuntimeError(f"{PUBLIC_MAX_FILE_MB}MB 제한 초과")

            orig_name = os.path.basename(getattr(f, "name", "upload.bin") or "upload.bin")
            orig_stem = Path(orig_name).stem

            content_type = (getattr(f, "content_type", "") or "").strip().lower()
            ext = _guess_ext(orig_name, content_type)

            ts = timezone.now().strftime("%Y%m%d%H%M%S%f")
            new_name = f"{ts}_{uuid.uuid4().hex}{ext}"

            tmp_path = tmp_dir / new_name
            with open(tmp_path, "wb") as out:
                for chunk in f.chunks():
                    out.write(chunk)

            mime = content_type or (mimetypes.guess_type(str(tmp_path))[0] or "application/octet-stream")

            # 캡션/태그 기본값
            if common_caption:
                caption_final = common_caption
            elif use_caption_from_name and orig_stem:
                caption_final = orig_stem
            else:
                caption_final = ""
            tags_final = (common_tags or "").strip()

            # AI 보강
            if PUBLIC_IMAGE_UPLOAD_AI and (not caption_final or not tags_final):
                ai_cap, ai_tags, ai_meta = ai_caption_tags_from_image(file_path=str(tmp_path), mime=mime)
                ai_used = bool(ai_meta.get("ai_used"))
                ai_model = str(ai_meta.get("ai_model") or "")
                ai_error = str(ai_meta.get("ai_error") or "")

                if ai_cap and not caption_final:
                    caption_final = ai_cap

                if ai_tags:
                    tags_final = _merge_tags(tags_final, ai_tags, max_n=IMAGE_AI_MAX_TAGS)

                if ai_error:
                    msg = (msg + " / " if msg else "") + f"ai: {ai_error}"

            # sha16
            h = hashlib.sha256()
            with open(tmp_path, "rb") as rf2:
                for c in iter(lambda: rf2.read(8192), b""):
                    h.update(c)
            sha16 = h.hexdigest()[:16]

            # pending blob 저장
            pending_prefix = f"pending/images/{timezone.now().strftime('%Y/%m')}"
            rel_key = f"{pending_prefix}/{new_name}"

            with open(tmp_path, "rb") as rf:
                storage_key = default_storage.save(rel_key, File(rf))
                storage_key = _normalize_storage_key(str(storage_key))

            # pending meta 저장
            pending_id = uuid.uuid4().hex
            pending_meta_key = f"{PENDING_UPLOAD_META_PREFIX}/{pending_id}.json"
            pending_payload = {
                "pending_id": pending_id,
                "storage_key": storage_key,
                "orig_name": orig_name,
                "orig_stem": orig_stem,
                "mime": mime,
                "size": int(size_bytes),
                "sha16": sha16,
                "actor_key": actor_key,
                "caption": caption_final,
                "tags": tags_final,
                "ai_used": bool(ai_used),
                "ai_model": ai_model,
                "ai_error": ai_error,
                "created_at": timezone.now().isoformat(),
            }
            default_storage.save(
                pending_meta_key,
                ContentFile(json.dumps(pending_payload, ensure_ascii=False).encode("utf-8")),
            )

            pid = pending_id
            indexed_ok = False  # pending 단계 인덱싱 X

            # (선택) chroma 상태 표시용 (디버그 표시에만)
            try:
                from ragapp.services.chroma_media import images_coll

                cdbg = images_coll()
                chroma_col = getattr(cdbg, "name", None)
                chroma_count = int(cdbg.count())
                if pid:
                    got = cdbg.get(ids=[pid], include=["metadatas"])  # type: ignore
                    indexed_ok = bool((got or {}).get("ids"))
            except Exception as _e:
                msg = (msg + " / " if msg else "") + f"chroma-check: {_e.__class__.__name__}: {_e}"

            if getattr(request, "user", None) and request.user.is_staff:
                url = _safe_media_url(str(storage_key))
                if not url:
                    msg = (msg + " / " if msg else "") + "URL 생성 실패"
            else:
                url = None

            ok += 1

        except Exception as e:
            status = "FAIL"
            msg = f"{e.__class__.__name__}: {e}"
            fail += 1
            url = None
            log.exception("media_index_view 업로드 실패: %s", msg)

            # 실패 시 pending blob 정리 시도
            try:
                if storage_key and default_storage.exists(storage_key):
                    default_storage.delete(storage_key)
            except Exception:
                pass

        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass

        cards.append(
            {
                "status": status,
                "msg": msg,
                "pid": pid,
                "url": url,
                "mime": mime,
                "size": int(size_bytes),
                "sha16": sha16,
                "actor_key": actor_key,
                "storage_key": storage_key,
                "indexed_ok": indexed_ok,
                "chroma_col": chroma_col,
                "chroma_count": chroma_count,
                "caption": caption_final,
                "tags": tags_final,
                "ai_used": ai_used,
                "ai_model": ai_model,
                "ai_error": ai_error,
            }
        )

    _log(
        request,
        "media_index",
        f"{len(files)} files",
        True,
        {"ok": ok, "fail": fail, "ai_on_upload": PUBLIC_IMAGE_UPLOAD_AI},
    )

    # ✅ 업로드로 pending가 생기면: staff는 검수 큐로, 일반은 접수확인으로
    pending_ids = [c.get("pid") for c in cards if c.get("status") == "PENDING" and c.get("pid")]

    if pending_ids:
        receipt_url = reverse("media_index") + "?pending=" + ",".join([str(x) for x in pending_ids if x])
        review_url = reverse("ragadmin_media_pending")

        is_staff = bool(getattr(request, "user", None) and request.user.is_staff)
        target_url = review_url if is_staff else receipt_url

        wants_json = (
            "application/json" in (request.headers.get("Accept", "") or "")
            or (request.headers.get("X-Requested-With", "") == "XMLHttpRequest")
        )
        if wants_json:
            return JsonResponse(
                {
                    "ok": True,
                    "ok_count": ok,
                    "fail_count": fail,
                    "cards": cards,
                    "pending_ids": pending_ids,
                    "redirect_url": target_url,
                },
                status=200 if fail == 0 else 207,
                json_dumps_params={"ensure_ascii": False},
            )

        return redirect(target_url)

    return render(
        request,
        "ragapp/media_index.html",
        {
            "allow_upload": PUBLIC_ALLOW_UPLOAD_IMAGES,
            "max_files": PUBLIC_MAX_FILES,
            "max_file_mb": PUBLIC_MAX_FILE_MB,
            "cards": cards,
            "ok": ok,
            "fail": fail,
            "ai_on_upload": PUBLIC_IMAGE_UPLOAD_AI,
        },
    )


# ────────────────────────────────────────────────
# (B) Staff: 어드민 업로드(즉시 승격+인덱싱)  ✅ “살아있는” 경로
# ────────────────────────────────────────────────
@never_cache
@staff_member_required
@require_http_methods(["GET", "POST"])
def media_upload_admin_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        return render(
            request,
            "ragadmin/media_upload_admin.html",
            {
                "max_files": PUBLIC_MAX_FILES,
                "max_file_mb": PUBLIC_MAX_FILE_MB,
                "ai_on_upload": PUBLIC_IMAGE_UPLOAD_AI,
            },
        )

    if add_image_item is None:
        return render(
            request,
            "ragadmin/media_upload_admin.html",
            {"error": f"chroma_media import 실패: {CHROMA_MEDIA_IMPORT_ERR or 'unknown'}"},
            status=500,
        )

    files = request.FILES.getlist("images")[:PUBLIC_MAX_FILES]
    use_caption_from_name = bool(request.POST.get("caption_from_name"))
    common_caption = (request.POST.get("caption") or "").strip()
    common_tags = (request.POST.get("tags") or "").strip()

    cards: List[Dict[str, Any]] = []
    ok, fail = 0, 0
    tmp_dir = _tmp_upload_dir()

    for f in files:
        status = "OK"
        msg = ""
        pid = ""
        url = ""
        mime = ""
        sha16 = ""
        storage_key = ""

        caption_final = ""
        tags_final = ""
        ai_used = False
        ai_model = ""
        ai_error = ""

        size_bytes = int(getattr(f, "size", 0) or 0)
        tmp_path: Optional[Path] = None

        try:
            if size_bytes > PUBLIC_MAX_FILE_MB * 1024 * 1024:
                raise RuntimeError(f"{PUBLIC_MAX_FILE_MB}MB 제한 초과")

            orig_name = os.path.basename(getattr(f, "name", "upload.bin") or "upload.bin")
            orig_stem = Path(orig_name).stem
            content_type = (getattr(f, "content_type", "") or "").strip().lower()
            ext = _guess_ext(orig_name, content_type)

            ts = timezone.now().strftime("%Y%m%d%H%M%S%f")
            new_name = f"{ts}_{uuid.uuid4().hex}{ext}"

            tmp_path = tmp_dir / new_name
            with open(tmp_path, "wb") as out:
                for chunk in f.chunks():
                    out.write(chunk)

            mime = content_type or (mimetypes.guess_type(str(tmp_path))[0] or "application/octet-stream")

            # 캡션/태그 기본값
            if common_caption:
                caption_final = common_caption
            elif use_caption_from_name and orig_stem:
                caption_final = orig_stem
            else:
                caption_final = ""
            tags_final = (common_tags or "").strip()

            # AI 보강
            if PUBLIC_IMAGE_UPLOAD_AI and (not caption_final or not tags_final):
                ai_cap, ai_tags, ai_meta = ai_caption_tags_from_image(file_path=str(tmp_path), mime=mime)
                ai_used = bool(ai_meta.get("ai_used"))
                ai_model = str(ai_meta.get("ai_model") or "")
                ai_error = str(ai_meta.get("ai_error") or "")
                if ai_cap and not caption_final:
                    caption_final = ai_cap
                if ai_tags:
                    tags_final = _merge_tags(tags_final, ai_tags, max_n=IMAGE_AI_MAX_TAGS)

            # sha16
            h = hashlib.sha256()
            with open(tmp_path, "rb") as rf2:
                for c in iter(lambda: rf2.read(8192), b""):
                    h.update(c)
            sha16 = h.hexdigest()[:16]

            # approved blob 저장(즉시)
            dt = timezone.now()
            dest_prefix = f"images/{dt.strftime('%Y/%m')}"
            dest_key = f"{dest_prefix}/{new_name}"

            with open(tmp_path, "rb") as rf:
                storage_key = default_storage.save(dest_key, File(rf))
                storage_key = _normalize_storage_key(str(storage_key))

            # 임베딩 + 인덱싱
            vec = embed_image_file(str(tmp_path), mime=mime)
            extra_meta: Dict[str, Any] = {
                "orig_name": orig_name,
                "orig_stem": orig_stem,
                "caption_final": caption_final,
                "tags_final": tags_final,
                "approved_at": timezone.now().isoformat(),
                "approved_storage_key": storage_key,
                "ai_used": bool(ai_used),
                "ai_model": ai_model,
                "sha16": sha16,
            }

            try:
                pid = add_image_item(
                    path=str(storage_key),
                    embedding=vec,
                    caption=caption_final,
                    original_name=orig_name,
                    tags=tags_final,
                    extra_meta=extra_meta,
                )
            except TypeError:
                pid = add_image_item(path=str(storage_key), embedding=vec, caption=caption_final, extra_meta=extra_meta)

            # public 승인(프록시에서 공개 허용)
            try:
                _approve_public_storage_key(storage_key)
            except Exception:
                pass

            # DB 키워드 테이블(있으면)
            try:
                from ragapp.services.media_db_keywords import upsert_image_meta_db

                upsert_image_meta_db(storage_key=storage_key, caption=caption_final, tags=tags_final)
            except Exception:
                pass

            url = _safe_media_url(storage_key) or ""
            if not url:
                msg = "URL 생성 실패"

            ok += 1

        except Exception as e:
            status = "FAIL"
            msg = f"{e.__class__.__name__}: {e}"
            fail += 1
            log.exception("media_upload_admin_view 실패: %s", msg)

            # 실패 시 저장된 blob 정리 시도
            try:
                if storage_key and default_storage.exists(storage_key):
                    default_storage.delete(storage_key)
            except Exception:
                pass

        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass

        cards.append(
            {
                "status": status,
                "msg": msg,
                "pid": str(pid),
                "url": url,
                "mime": mime,
                "size": int(size_bytes),
                "sha16": sha16,
                "storage_key": storage_key,
                "caption": caption_final,
                "tags": tags_final,
                "ai_used": bool(ai_used),
                "ai_model": ai_model,
                "ai_error": ai_error,
            }
        )

    wants_json = (
        "application/json" in (request.headers.get("Accept", "") or "")
        or (request.headers.get("X-Requested-With", "") == "XMLHttpRequest")
    )
    if wants_json:
        return JsonResponse(
            {"ok": True, "ok_count": ok, "fail_count": fail, "cards": cards},
            status=200 if fail == 0 else 207,
            json_dumps_params={"ensure_ascii": False},
        )

    return render(
        request,
        "ragadmin/media_upload_admin.html",
        {
            "max_files": PUBLIC_MAX_FILES,
            "max_file_mb": PUBLIC_MAX_FILE_MB,
            "ai_on_upload": PUBLIC_IMAGE_UPLOAD_AI,
            "cards": cards,
            "ok": ok,
            "fail": fail,
        },
    )


# ────────────────────────────────────────────────
# (C) 텍스트→이미지 검색  ✅ 429 하드 제한
# ────────────────────────────────────────────────
@never_cache
def media_search_view(request: HttpRequest) -> HttpResponse:
    delete_pid = request.GET.get("delete")
    if delete_pid and getattr(request, "user", None) and request.user.is_staff:
        try:
            _delete_image_from_index(str(delete_pid))
        except Exception:
            pass
        qs = request.GET.copy()
        qs.pop("delete", None)
        return redirect(f"{request.path}?{qs.urlencode()}" if qs else request.path)

    q = (request.GET.get("q") or "").strip()

    raw_size = _int(request.GET.get("size"), 5)
    size = max(1, min(raw_size, 5))  # ✅ page당 최대 5개 강제

    page = max(1, _int(request.GET.get("page"), 1))
    k = max(1, min(_int(request.GET.get("k"), 120), 600))

    hits: List[Dict[str, Any]] = []

    if not q:
        return render(
            request,
            "ragapp/media_search.html",
            {
                "q": q,
                "size": size,
                "page": page,
                "k": k,
                "hits": hits,
                "has_prev": False,
                "has_next": False,
                "offer_show_low": False,
                "offer_show_low_message": "",
            },
        )

    base_ctx = {"q": q, "size": size, "page": page, "k": k}
    resp = _enforce_quota_or_429(
        request,
        kind="image",
        template_name="ragapp/media_search.html",
        base_ctx={
            **base_ctx,
            "hits": [],
            "has_prev": False,
            "has_next": False,
            "offer_show_low": False,
            "offer_show_low_message": "",
        },
        msg="오늘 사용할 수 있는 이미지 검색 횟수를 모두 사용했습니다. 내일 다시 이용해 주세요.",
    )
    if resp is not None:
        return resp

    if search_images_by_text_embedding is None:
        return render(
            request,
            "ragapp/media_search.html",
            {**base_ctx, "error": "이미지 검색 모듈을 불러올 수 없습니다(chroma_media import 실패).", "hits": []},
            status=500,
        )

    deleted_ids = _load_deleted_image_ids()
    debug_json = None

    is_staff = bool(getattr(request, "user", None) and request.user.is_staff)
    debug_enabled = is_staff and (request.GET.get("debug") == "1")
    auto_prune = is_staff and (request.GET.get("auto_prune") == "1")

    show_low = False
    if is_staff:
        show_low = (request.GET.get("show_low") or "").strip().lower() in ("1", "true", "yes", "on")

    missing_key_count = 0
    missing_key_samples: list[dict[str, str]] = []

    auto_fix = debug_enabled and (request.GET.get("auto_fix") == "1")
    fixed_meta_count = 0
    fixed_meta_samples: list[dict[str, str]] = []

    offer_show_low = False
    offer_show_low_message = ""

    try:
        _SYN: Dict[str, List[str]] = {
            "쇼파": ["소파"],
            "소파": ["쇼파"],
            "거실": ["리빙룸", "livingroom", "living room"],
        }
        _STOP = {
            "a",
            "an",
            "the",
            "of",
            "to",
            "in",
            "on",
            "for",
            "with",
            "and",
            "or",
            "is",
            "are",
            "was",
            "were",
            "사진",
            "이미지",
            "그림",
            "검색",
            "찾아줘",
            "관련",
            "같은",
            "느낌",
            "비슷한",
        }

        def _norm(s: str) -> str:
            s = (s or "").strip().lower()
            s = re.sub(r"\s+", " ", s)
            return s

        def _tokenize(qs: str) -> List[str]:
            qs = _norm(qs)
            parts = re.findall(r"[0-9a-zA-Z가-힣_\-]+", qs)
            out: List[str] = []
            seen = set()
            for t in parts:
                if len(t) < 2:
                    continue
                if t in _STOP:
                    continue
                if t in seen:
                    continue
                seen.add(t)
                out.append(t)

                if t in _SYN:
                    for alt in _SYN[t]:
                        aa = _norm(alt)
                        if aa and aa not in seen:
                            seen.add(aa)
                            out.append(aa)
            return out

        def _extract_phrase(qs: str) -> Optional[str]:
            m = re.search(r'"([^"]+)"', qs)
            if m and m.group(1).strip():
                return _norm(m.group(1))
            m = re.search(r"'([^']+)'", qs)
            if m and m.group(1).strip():
                return _norm(m.group(1))
            return None

        try:
            _space = (images_space() or "").lower()
        except Exception:
            _space = "cosine"
        if _space not in ("cosine", "l2", "ip", "inner_product"):
            _space = "cosine"

        def _distance_to_similarity(d: Optional[float]) -> float:
            if d is None:
                return 0.0
            try:
                dd = float(d)
            except Exception:
                return 0.0
            if dd < 0.0:
                dd = 0.0

            if _space in ("cosine",):
                sim = 1.0 - dd
                return float(max(0.0, min(1.0, sim)))

            if _space in ("l2", "ip", "inner_product"):
                return float(1.0 / (1.0 + dd))

            if dd <= 2.0:
                sim = 1.0 - dd
                return float(max(0.0, min(1.0, sim)))
            return float(1.0 / (1.0 + dd))

        def _meta_text(meta: Dict[str, Any], doc: str) -> str:
            chunks: List[str] = []
            if doc:
                chunks.append(doc)

            for k_ in (
                "search_text",
                "caption",
                "title",
                "name",
                "filename",
                "file_name",
                "basename",
                "orig_name",
                "orig_stem",
                "original_name",
                "source",
            ):
                v = meta.get(k_)
                if isinstance(v, str) and v.strip():
                    chunks.append(v)

            for k_ in ("path", "filepath"):
                v = meta.get(k_)
                if isinstance(v, str) and v.strip():
                    chunks.append(Path(v.strip()).name)

            tags = meta.get("tags")
            if isinstance(tags, str) and tags.strip():
                chunks.append(tags)
            elif isinstance(tags, list):
                chunks.append(" ".join([str(x) for x in tags if x is not None]))

            return _norm(" ".join(chunks))

        def _keyword_score(text: str, keywords: List[str], phrase: Optional[str]) -> float:
            if not text:
                return 0.0
            bonus = 0.0
            if phrase and phrase in text:
                bonus += 0.35
            hits_ = 0
            for kw_ in keywords:
                if kw_ and (kw_ in text):
                    hits_ += 1
            base = (hits_ / max(1, len(keywords))) if keywords else 0.0
            score_ = min(1.0, base + bonus)
            return float(score_)

        def _all_keywords_present(text: str, keywords: List[str]) -> bool:
            if not text or not keywords:
                return False
            return all((kw in text) for kw in keywords)

        def _unpack(res_: Dict[str, Any]) -> Tuple[List[Any], List[Any], List[Any], List[Any]]:
            ids_ = (res_.get("ids") or [[]])[0]
            metas_ = (res_.get("metadatas") or [[]])[0]
            docs_ = (res_.get("documents") or [[]])[0]
            dists_ = (res_.get("distances") or [[]])[0]
            return ids_, metas_, docs_, dists_

        qv = embed_text_mm(q)
        fetch_k = min(1200, max(k, page * size * 12, page * size + 200, 200))

        res = search_images_by_text_embedding(text_embedding=qv, k=fetch_k) or {}
        ids, metas, docs, dists = _unpack(res)

        keywords = _tokenize(q)
        phrase = _extract_phrase(q)

        mode = (request.GET.get("mode") or "hybrid").strip().lower()
        if mode not in ("hybrid", "vec", "exact"):
            mode = "hybrid"

        if is_staff:
            try:
                min_vec_sim = float(request.GET.get("min_sim", "") or 0.22)
            except Exception:
                min_vec_sim = 0.22
        else:
            min_vec_sim = 0.22

        min_vec_sim = max(0.0, min(min_vec_sim, 0.95))
        if is_staff and show_low:
            min_vec_sim = 0.0

        w_vec, w_kw = 0.75, 0.25
        if mode == "vec":
            keywords = []
            phrase = None
            w_vec, w_kw = 1.0, 0.0
        elif mode == "exact":
            w_vec, w_kw = 0.25, 0.75

        raw_candidates: List[Dict[str, Any]] = []

        for rank, pid in enumerate(ids):
            spid = str(pid)
            if spid in deleted_ids:
                continue

            meta = metas[rank] if rank < len(metas) and isinstance(metas[rank], dict) else {}
            doc = docs[rank] if rank < len(docs) and isinstance(docs[rank], str) else ""

            dist = dists[rank] if rank < len(dists) else None
            vec_sim = _distance_to_similarity(dist)

            raw_path = _pick_raw_path(meta, doc).strip()
            if raw_path.strip().lower() in ("", "-", "none"):
                continue

            real_key = _resolve_existing_storage_key(raw_path)
            if not real_key:
                missing_key_count += 1
                if debug_enabled and len(missing_key_samples) < 30:
                    missing_key_samples.append(
                        {
                            "pid": spid,
                            "raw_path": raw_path,
                            "meta_path": (meta.get("path") or ""),
                            "meta_filepath": (meta.get("filepath") or ""),
                            "meta_url": (meta.get("url") or ""),
                            "meta_storage_key": (meta.get("storage_key") or meta.get("key") or ""),
                        }
                    )
                if auto_prune:
                    try:
                        _delete_image_from_index(spid)
                    except Exception:
                        pass
                continue

            if auto_fix and raw_path and (str(raw_path).strip() != str(real_key).strip()):
                try:
                    from ragapp.services.chroma_media import images_coll

                    coll = images_coll()
                    new_meta = dict(meta or {})
                    new_meta["path"] = str(real_key)
                    new_meta["filepath"] = str(real_key)
                    new_meta["storage_key"] = str(real_key)
                    new_meta["fixed_from"] = str(raw_path)
                    coll.update(ids=[spid], metadatas=[new_meta])
                    fixed_meta_count += 1
                    if len(fixed_meta_samples) < 30:
                        fixed_meta_samples.append({"pid": str(spid), "from": str(raw_path), "to": str(real_key)})
                except Exception:
                    pass

            path = real_key

            if not is_public_image(str(path)):
                continue

            url = ""
            if is_staff:
                url = _safe_media_url(path) or ""

            text = _meta_text(meta, doc)
            kw = _keyword_score(text, keywords, phrase)

            raw_candidates.append(
                {
                    "pid": pid,
                    "meta": meta,
                    "doc": doc,
                    "path": path,
                    "storage_key": real_key,
                    "url": url,  # public이면 ""로 유지
                    "vec_sim": float(vec_sim),
                    "kw": float(kw),
                    "text": text,
                }
            )

        if not raw_candidates:
            return render(
                request,
                "ragapp/media_search.html",
                {
                    "mode": mode,
                    "q": q,
                    "size": size,
                    "page": page,
                    "k": k,
                    "hits": [],
                    "has_prev": False,
                    "has_next": False,
                    "debug_json": debug_json,
                    "offer_show_low": False,
                    "offer_show_low_message": "",
                },
            )

        top_vec = max((c["vec_sim"] for c in raw_candidates), default=0.0)
        dynamic_floor = max(min_vec_sim, top_vec - 0.30)
        if is_staff and show_low:
            dynamic_floor = 0.0

        LOW_RELEVANCE_FLOOR = 0.20
        candidates: List[Dict[str, Any]] = []

        if is_staff and show_low:
            candidates = list(raw_candidates)
        else:
            if mode == "vec":
                candidates = list(raw_candidates) if debug_enabled else [c for c in raw_candidates if c["vec_sim"] >= dynamic_floor]
            elif mode == "exact":
                if phrase:
                    candidates = [c for c in raw_candidates if phrase in (c.get("text") or "")]
                else:
                    if keywords:
                        candidates = [c for c in raw_candidates if _all_keywords_present(c.get("text") or "", keywords)]
                    else:
                        candidates = []
                        offer_show_low = True
                        offer_show_low_message = "정확 모드로 검색하기엔 검색어가 너무 짧아요."
                if (not candidates) and (not offer_show_low):
                    offer_show_low = True
                    offer_show_low_message = "정확히 일치하는 결과가 없어요."
            else:
                if keywords:
                    candidates = [c for c in raw_candidates if (c["kw"] > 0.0) or (c["vec_sim"] >= dynamic_floor)]
                else:
                    candidates = [c for c in raw_candidates if c["vec_sim"] >= dynamic_floor]

                if (not debug_enabled) and (top_vec < LOW_RELEVANCE_FLOOR) and (
                    not any((c.get("kw") or 0.0) > 0.0 for c in raw_candidates)
                ):
                    candidates = []
                    if not is_staff:
                        offer_show_low = False
                        offer_show_low_message = ""

        for c in candidates:
            c["score"] = (w_vec * float(c.get("vec_sim") or 0.0)) + (w_kw * float(c.get("kw") or 0.0))
        candidates.sort(key=lambda x: x.get("score", 0.0), reverse=True)

        if len(candidates) > int(k):
            candidates = candidates[: int(k)]

        start = (page - 1) * size
        end = start + size
        page_items = candidates[start:end]

        hits = []
        for c in page_items:
            meta = c.get("meta") or {}
            doc = c.get("doc") or ""
            caption = (
                doc
                or meta.get("caption")
                or meta.get("original_name")
                or meta.get("orig_name")
                or meta.get("basename")
                or "(캡션 없음)"
            )
            sk = (c.get("storage_key") or c.get("path") or "").strip()

            # ✅ same-origin 프록시 URL 우선(대부분 CSP 통과) → 실패 시 signed fallback
            proxy = _safe_media_url(sk) or ""  # /uploads/... 같은 경로가 나오면 이게 1순위
            signed = _signed_url_for_storage_key(sk, expires_sec=1800) if sk else ""
            img_url = proxy or signed

            if is_staff:
                pid_val = c.get("pid")
                pid_str = str(pid_val) if pid_val is not None else ""
                hits.append(
                    {
                        "pid": pid_str,
                        "caption": caption,
                        "path": c.get("path") or "",
                        "storage_key": c.get("storage_key") or "",
                        "signed_url": img_url,   # ✅ 화면에 찍을 URL은 img_url로
                        "url": (c.get("url") or ""),
                    }
                )
            else:
                hits.append(
                    {
                        "caption": caption,
                        "signed_url": img_url,   # ✅ public도 이제 프록시 우선
                    }
                )

        has_any_image = False
        if is_staff:
            has_any_image = any(((h.get("signed_url") or h.get("url") or "").strip()) for h in hits)
        else:
            has_any_image = any(((h.get("signed_url") or "").strip()) for h in hits)

        total = len(candidates)
        has_prev = page > 1 and (start < total)
        has_next = end < total

        if hits and (not has_any_image):
            hits = []
            has_prev = False
            has_next = False
            offer_show_low = False
            offer_show_low_message = ""

        if not is_staff:
            offer_show_low = False
            offer_show_low_message = ""

        wants_json = (
            "application/json" in (request.headers.get("Accept", "") or "")
            or (request.headers.get("X-Requested-With", "") == "XMLHttpRequest")
        )
        if wants_json:
            if is_staff:
                payload_hits = hits
            else:
                payload_hits = [
                    {
                        "caption": (h.get("caption") or ""),
                        "signed_url": (h.get("signed_url") or ""),
                    }
                    for h in hits
                ]

            payload = {
                "ok": True,
                "mode": mode,
                "q": q,
                "size": size,
                "page": page,
                "k": k,
                "hits": payload_hits,
                "has_prev": has_prev,
                "has_next": has_next,
            }

            if is_staff:
                payload["offer_show_low"] = bool(offer_show_low)
                payload["offer_show_low_message"] = offer_show_low_message
                payload["show_low"] = bool(show_low)

            return JsonResponse(payload, status=200, json_dumps_params={"ensure_ascii": False})

        return render(
            request,
            "ragapp/media_search.html",
            {
                "mode": mode,
                "q": q,
                "size": size,
                "page": page,
                "k": k,
                "hits": hits,
                "has_prev": has_prev,
                "has_next": has_next,
                "debug_json": debug_json,
                "offer_show_low": offer_show_low,
                "offer_show_low_message": offer_show_low_message,
            },
        )

    except Exception as e:
        return render(
            request,
            "ragapp/media_search.html",
            {
                "mode": (request.GET.get("mode") or "hybrid").strip().lower(),
                "q": q,
                "size": size,
                "page": page,
                "k": k,
                "error": str(e),
                "hits": [],
                "has_prev": False,
                "has_next": False,
                "debug_json": debug_json,
                "offer_show_low": False,
                "offer_show_low_message": "",
            },
        )


# ────────────────────────────────────────────────
# (API) 이미지 메타 업서트 (기존 유지)
# ────────────────────────────────────────────────
@require_POST
@csrf_protect
def api_media_upsert_tags_caption(request: HttpRequest) -> JsonResponse:
    u = getattr(request, "user", None)
    if not (u and u.is_authenticated and u.is_staff):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403, json_dumps_params={"ensure_ascii": False})

    try:
        if request.content_type and "application/json" in request.content_type.lower():
            raw = (request.body or b"").decode("utf-8") or ""
            payload: Dict[str, Any] = json.loads(raw) if raw else {}
        else:
            payload = request.POST.dict()
            try:
                if "tags" in request.POST:
                    tl = request.POST.getlist("tags")
                    if len(tl) > 1:
                        payload["tags"] = tl
                    elif len(tl) == 1:
                        payload["tags"] = tl[0]
            except Exception:
                pass
    except Exception:
        payload = request.POST.dict()

    path = (payload.get("path") or payload.get("key") or payload.get("storage_key") or "").strip()
    if not path:
        return JsonResponse({"ok": False, "error": "path_required"}, status=400)

    try:
        path = _normalize_storage_key(path)
    except Exception:
        path = (path or "").strip().replace("\\", "/").lstrip("/")

    if "caption" not in payload:
        caption = None
    else:
        raw_caption = payload.get("caption", None)
        caption = None if raw_caption is None else str(raw_caption).strip()

    if "tags" not in payload:
        tags: Union[str, List[str], None] = None
    else:
        raw_tags = payload.get("tags", None)
        if raw_tags is None:
            tags = None
        elif isinstance(raw_tags, str):
            tags = raw_tags.strip()
        elif isinstance(raw_tags, list):
            tags = [str(x).strip() for x in raw_tags if str(x).strip()]
        else:
            tags = str(raw_tags).strip()

    try:
        from ragapp.services.chroma_media import upsert_image_tags_caption, images_coll
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"import_failed: {e}"}, status=500)

    try:
        pid = upsert_image_tags_caption(path=path, caption=caption, tags=tags)

        caption_eff = ""
        tags_eff = ""
        meta_ok = False

        try:
            col = images_coll()
            got = col.get(ids=[pid], include=["metadatas"])
            metas = (got or {}).get("metadatas") or []
            if metas and isinstance(metas[0], dict):
                m0 = metas[0]
                cap_v = m0.get("caption") or m0.get("caption_final") or ""
                caption_eff = str(cap_v or "").strip()

                tv = m0.get("tags")
                if tv is None:
                    tv = m0.get("tags_final")

                if isinstance(tv, list):
                    tags_eff = ", ".join([str(x).strip() for x in tv if str(x).strip()])
                else:
                    tags_eff = str(tv or "").strip()

                meta_ok = True
        except Exception:
            meta_ok = False

        if meta_ok:
            try:
                from ragapp.services.media_db_keywords import upsert_image_meta_db

                upsert_image_meta_db(storage_key=path, caption=caption_eff, tags=tags_eff)
            except Exception as e:
                log.exception("DB meta upsert failed: %s", e)

        resp = {"ok": True, "pid": pid, "meta": {"caption": caption_eff, "tags": tags_eff}}
        if not meta_ok:
            resp["warning"] = "chroma_meta_read_failed_db_not_updated"

        return JsonResponse(resp, status=200, json_dumps_params={"ensure_ascii": False})

    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=500, json_dumps_params={"ensure_ascii": False})


@require_GET
def api_media_keyword_search(request: HttpRequest) -> JsonResponse:
    q = (request.GET.get("q") or "").strip()
    limit = _int(request.GET.get("limit"), 60)
    limit = max(1, min(limit, 200))

    if not q:
        return JsonResponse({"ok": True, "q": q, "hits": []}, status=200, json_dumps_params={"ensure_ascii": False})

    is_staff = bool(getattr(request, "user", None) and request.user.is_staff)

    try:
        from ragapp.services.media_db_keywords import keyword_search_images_db

        hits = keyword_search_images_db(query=q, limit=limit)

        out: list[dict[str, Any]] = []
        for h in hits:
            key = (h.get("storage_key") or "").strip()
            if not key:
                continue

            # ✅ 승인된 공개 이미지인지 최종 필터 (public 누출 방지)
            try:
                if not is_public_image(key):
                    continue
            except Exception:
                continue

            proxy = _safe_media_url(key) or ""
            signed = _signed_url_for_storage_key(key, expires_sec=1800) if key else ""
            img_url = proxy or signed

            if is_staff:
                out.append(
                    {
                        **h,
                        "storage_key": key,
                        "signed_url": img_url,   # ✅ 통일
                        "url": proxy,            # (기존 유지)
                    }
                )
            else:
                out.append(
                    {
                        "caption": h.get("caption") or h.get("title") or "",
                        "tags": h.get("tags") or "",
                        "signed_url": img_url,   # ✅ public도 프록시 우선
                    }
                )

        return JsonResponse({"ok": True, "q": q, "hits": out}, status=200, json_dumps_params={"ensure_ascii": False})
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"{e.__class__.__name__}: {e}"}, status=500)


# ────────────────────────────────────────────────
# (D) Pending 검수 페이지 + API
# ────────────────────────────────────────────────
@never_cache
@staff_member_required
@require_GET
def media_pending_admin_view(request: HttpRequest) -> HttpResponse:
    limit = max(1, min(_int(request.GET.get("limit"), 60), 200))
    ids = _list_pending_ids(limit=limit)

    items: list[dict[str, Any]] = []
    for pid in ids:
        meta_key = f"{PENDING_UPLOAD_META_PREFIX}/{pid}.json"
        meta = _read_json_from_storage(meta_key) or {}
        storage_key = str(meta.get("storage_key") or "").strip()

        items.append(
            {
                "pending_id": pid,
                "meta_key": meta_key,
                "storage_key": storage_key,
                "actor_key": meta.get("actor_key") or "",
                "created_at": meta.get("created_at"),
                "orig_name": meta.get("orig_name") or "",
                "mime": meta.get("mime") or "",
                "size": meta.get("size") or 0,
                "sha16": meta.get("sha16") or "",
                "caption": meta.get("caption") or "",
                "tags": meta.get("tags") or "",
                "ai_used": bool(meta.get("ai_used")),
                "ai_model": meta.get("ai_model") or "",
                "ai_error": meta.get("ai_error") or "",
                "signed_url": _signed_url_for_storage_key(storage_key, expires_sec=1800) if storage_key else "",
                "preview_url": _pending_preview_url(request, pid),  # ✅ 여기!
            }
        )

    return render(
        request,
        "ragadmin/media_pending_admin.html",
        {
            "limit": limit,
            "count": len(items),
            "items": items,
            "approve_url": reverse("api_media_pending_approve"),
            "reject_url": reverse("api_media_pending_reject"),
            "lift_url": reverse("api_user_penalty_lift"),
            "penalty_list_url": reverse("api_user_penalty_list"),  # ✅ 드로어에서 사용
            "penalties_url": reverse("ragadmin_media_penalties"),
            "admin_upload_url": reverse("ragadmin_media_upload"),
            "rejected_url": reverse("ragadmin_media_rejected"),
        },
    )


@require_GET
@staff_member_required
def api_media_pending_list(request: HttpRequest) -> JsonResponse:
    limit = max(1, min(_int(request.GET.get("limit"), 60), 200))
    ids = _list_pending_ids(limit=limit)

    items: list[dict[str, Any]] = []
    for pid in ids:
        meta_key = f"{PENDING_UPLOAD_META_PREFIX}/{pid}.json"
        meta = _read_json_from_storage(meta_key) or {}
        storage_key = str(meta.get("storage_key") or "").strip()

        signed_url = _signed_url_for_storage_key(storage_key, expires_sec=1800) if storage_key else ""

        items.append(
            {
                "pending_id": pid,
                "meta_key": meta_key,
                "storage_key": storage_key,
                "created_at": meta.get("created_at"),
                "caption": meta.get("caption") or "",
                "tags": meta.get("tags") or "",
                "preview_url": _pending_preview_url(request, pid),  # ✅ 여기!
                "signed_url": signed_url,  # 옵션(디버깅용)
                "url": _safe_media_url(storage_key) or "",  # staff-only
            }
        )

    return JsonResponse({"ok": True, "count": len(items), "items": items}, status=200, json_dumps_params={"ensure_ascii": False})


@require_POST
@csrf_protect
@staff_member_required
def api_media_pending_approve(request: HttpRequest) -> JsonResponse:
    pending_id = ""
    caption_override = None
    tags_override = None
    try:
        if request.content_type and "application/json" in request.content_type.lower():
            raw = (request.body or b"").decode("utf-8") or ""
            payload = json.loads(raw) if raw else {}
            pending_id = str(payload.get("pending_id") or "").strip()
            if "caption" in payload:
                caption_override = str(payload.get("caption") or "").strip()
            if "tags" in payload:
                tags_override = str(payload.get("tags") or "").strip()
        else:
            pending_id = str(request.POST.get("pending_id") or "").strip()
            if "caption" in request.POST:
                caption_override = str(request.POST.get("caption") or "").strip()
            if "tags" in request.POST:
                tags_override = str(request.POST.get("tags") or "").strip()
    except Exception:
        pending_id = str(request.POST.get("pending_id") or "").strip()

    if not pending_id:
        return JsonResponse({"ok": False, "error": "pending_id_required"}, status=400)

    if add_image_item is None:
        return JsonResponse({"ok": False, "error": "add_image_item_unavailable"}, status=500)

    meta_key = f"{PENDING_UPLOAD_META_PREFIX}/{pending_id}.json"
    meta = _read_json_from_storage(meta_key) or {}
    if not meta:
        return JsonResponse({"ok": False, "error": "pending_meta_not_found"}, status=404)

    src_key = str(meta.get("storage_key") or "").strip()
    if not src_key:
        return JsonResponse({"ok": False, "error": "pending_storage_key_missing"}, status=400)

    mime = str(meta.get("mime") or "application/octet-stream")
    caption = str(meta.get("caption") or "").strip()
    tags = str(meta.get("tags") or "").strip()
    orig_name = str(meta.get("orig_name") or "").strip()
    orig_stem = str(meta.get("orig_stem") or "").strip()

    # ✅ 검수 화면에서 수정한 값 우선 반영
    if caption_override is not None:
        caption = caption_override
    if tags_override is not None:
        tags = tags_override

    dt = None
    try:
        ca = str(meta.get("created_at") or "").strip()
        if ca:
            dt = timezone.datetime.fromisoformat(ca.replace("Z", "+00:00"))
    except Exception:
        dt = None
    if dt is None:
        dt = timezone.now()

    dest_prefix = f"images/{dt.strftime('%Y/%m')}"

    try:
        dest_key, data = _promote_pending_object(src_key=src_key, dest_prefix=dest_prefix)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"promote_failed: {e}"}, status=500)

    tmp_dir = _tmp_upload_dir()
    tmp_path = tmp_dir / f"approve_{pending_id}_{uuid.uuid4().hex}_{Path(dest_key).name}"
    try:
        with open(tmp_path, "wb") as out:
            out.write(data)

        vec = embed_image_file(str(tmp_path), mime=mime)

        extra_meta: Dict[str, Any] = {
            "pending_id": pending_id,
            "orig_name": orig_name,
            "orig_stem": orig_stem,
            "caption_final": caption,
            "tags_final": tags,
            "approved_at": timezone.now().isoformat(),
            "approved_storage_key": dest_key,
            "ai_used": bool(meta.get("ai_used")),
            "ai_model": str(meta.get("ai_model") or ""),
            "sha16": str(meta.get("sha16") or ""),
        }

        try:
            pid = add_image_item(
                path=str(dest_key),
                embedding=vec,
                caption=caption,
                original_name=orig_name,
                tags=tags,
                extra_meta=extra_meta,
            )
        except TypeError:
            pid = add_image_item(path=str(dest_key), embedding=vec, caption=caption, extra_meta=extra_meta)

    except Exception as e:
        return JsonResponse({"ok": False, "error": f"index_failed: {e}"}, status=500)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass

    try:
        _approve_public_storage_key(dest_key)
    except Exception:
        pass

    try:
        from ragapp.services.media_db_keywords import upsert_image_meta_db

        upsert_image_meta_db(storage_key=dest_key, caption=caption, tags=tags)
    except Exception:
        pass

    approved_key = f"{APPROVED_UPLOAD_META_PREFIX}/{pending_id}.json"
    approved_payload = dict(meta)
    approved_payload.update(
        {
            "approved": True,
            "approved_at": timezone.now().isoformat(),
            "approved_storage_key": dest_key,
            "approved_pid": str(pid),
            "caption": caption,
            "tags": tags,
        }
    )
    _write_json_to_storage(approved_key, approved_payload)

    try:
        src_norm = _normalize_storage_key(src_key)
    except Exception:
        src_norm = (src_key or "").strip().replace("\\", "/").lstrip("/")
    try:
        if src_norm and src_norm.startswith("pending/") and default_storage.exists(src_norm):
            default_storage.delete(src_norm)
    except Exception:
        pass

    try:
        if default_storage.exists(meta_key):
            default_storage.delete(meta_key)
    except Exception:
        pass

    return JsonResponse(
        {"ok": True, "pending_id": pending_id, "pid": str(pid), "storage_key": dest_key, "url": _safe_media_url(dest_key) or ""},
        status=200,
        json_dumps_params={"ensure_ascii": False},
    )


@require_POST
@csrf_protect
@staff_member_required
def api_media_pending_reject(request: HttpRequest) -> JsonResponse:
    pending_id = ""
    payload: Dict[str, Any] = {}
    try:
        if request.content_type and "application/json" in request.content_type.lower():
            raw = (request.body or b"").decode("utf-8") or ""
            payload = json.loads(raw) if raw else {}
            pending_id = str(payload.get("pending_id") or "").strip()
        else:
            payload = request.POST.dict()
            pending_id = str(request.POST.get("pending_id") or "").strip()
    except Exception:
        payload = request.POST.dict()
        pending_id = str(request.POST.get("pending_id") or "").strip()

    if not pending_id:
        return JsonResponse({"ok": False, "error": "pending_id_required"}, status=400, json_dumps_params={"ensure_ascii": False})

    reason = str(payload.get("reason") or "").strip()
    reject_mode = str(payload.get("reject_mode") or "reject_only").strip()  # reject_only | restrict_all
    restrict_days_raw = str(payload.get("restrict_days") or "7").strip()  # 1/7/14/21/30/permanent
    allowed_days = {1, 7, 14, 21, 30}
    permaban = False
    suspend_days = 0
    if reject_mode == "restrict_all":
        if restrict_days_raw.lower() in ("permanent", "perm", "forever", "영구"):
            permaban = True
        else:
            try:
                d = int(restrict_days_raw)
            except Exception:
                d = 7
            suspend_days = d if d in allowed_days else 7

    meta_key = f"{PENDING_UPLOAD_META_PREFIX}/{pending_id}.json"
    meta = _read_json_from_storage(meta_key) or {}
    if not meta:
        return JsonResponse({"ok": False, "error": "pending_meta_not_found"}, status=404, json_dumps_params={"ensure_ascii": False})

    src_key = _normalize_storage_key(str(meta.get("storage_key") or "").strip())
    actor_key = str(meta.get("actor_key") or "").strip()

    # ImageUploadRequest가 있으면 기록(있을 때만)
    upload_request = None
    try:
        from ragapp.moderation_models import ImageUploadRequest

        upload_request = ImageUploadRequest.objects.filter(storage_key=src_key).first()
        if upload_request:
            actor_key = actor_key or str(getattr(upload_request, "actor_key", "") or "")
            upload_request.status = ImageUploadRequest.Status.REJECTED
            upload_request.reject_reason = reason
            upload_request.reviewed_by = getattr(request, "user", None)
            upload_request.reviewed_at = timezone.now()
            upload_request.save(update_fields=["status", "reject_reason", "reviewed_by", "reviewed_at", "updated_at"])
    except Exception:
        pass

    # ✅ 제재 적용
    if reject_mode == "restrict_all" and actor_key:
        try:
            from ragapp.moderation_models import UserPenalty

            reviewer = getattr(request, "user", None)

            if permaban:
                if hasattr(UserPenalty, "apply_permaban_all"):
                    UserPenalty.apply_permaban_all(actor_key, by_user=reviewer, reason=reason or "영구 제한")
                elif hasattr(UserPenalty, "apply_permaban"):
                    UserPenalty.apply_permaban(actor_key, by_user=reviewer, reason=reason or "영구 제한")
                else:
                    try:
                        UserPenalty.objects.create(actor_key=actor_key, kind="permaban", reason=reason or "영구 제한", created_by=reviewer)
                    except Exception:
                        pass
            else:
                if hasattr(UserPenalty, "apply_suspend_all"):
                    UserPenalty.apply_suspend_all(actor_key, days=int(suspend_days), by_user=reviewer, reason=reason or "기간 제한")
                elif hasattr(UserPenalty, "apply_suspend"):
                    UserPenalty.apply_suspend(actor_key, days=int(suspend_days), by_user=reviewer, reason=reason or "기간 제한")
                else:
                    try:
                        until = timezone.now() + timezone.timedelta(days=int(suspend_days))
                        UserPenalty.objects.create(actor_key=actor_key, kind="suspend", until=until, reason=reason or "기간 제한", created_by=reviewer)
                    except Exception:
                        pass
        except Exception:
            pass

    # pending blob 삭제
    rejected_storage_key = src_key
    if src_key and src_key.startswith("pending/"):
        try:
            rejected_storage_key, _ = _promote_pending_object(
                src_key=src_key,
                dest_prefix=f"rejected/{timezone.now().strftime('%Y/%m')}",
            )
        except Exception:
            # Retain the original object if quarantine relocation fails, but log it
            # loudly: otherwise the pending blob becomes an orphan (meta says
            # "rejected", file stays under pending/) with no trace for ops to find.
            log.warning(
                "quarantine relocation failed for pending_id=%s src_key=%s; "
                "file left in place, meta will still be marked rejected",
                pending_id, src_key, exc_info=True,
            )
            rejected_storage_key = src_key

    if upload_request is not None and rejected_storage_key != src_key:
        try:
            upload_request.storage_key = rejected_storage_key
            upload_request.save(update_fields=["storage_key", "updated_at"])
        except Exception:
            pass

    # rejected meta 저장
    try:
        rejected_key = f"{REJECTED_UPLOAD_META_PREFIX}/{pending_id}.json"
        rejected_payload = dict(meta)
        rejected_payload.update(
            {
                "rejected": True,
                "rejected_at": timezone.now().isoformat(),
                "storage_key": rejected_storage_key,
                "rejected_storage_key": rejected_storage_key,
                "reject_reason": reason,
                "reject_mode": reject_mode,
                "restrict": {"permaban": bool(permaban), "days": int(suspend_days or 0)},
                "delete_blob": False,
                "retained_for_review": True,
            }
        )
        _write_json_to_storage(rejected_key, rejected_payload)
    except Exception:
        pass

    try:
        if default_storage.exists(meta_key):
            default_storage.delete(meta_key)
    except Exception:
        pass

    return JsonResponse({"ok": True, "pending_id": pending_id, "actor_key": actor_key}, status=200, json_dumps_params={"ensure_ascii": False})


def _safe_review_id(value: str) -> str:
    value = str(value or "").strip()
    return value if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) else ""


@never_cache
@staff_member_required
@require_GET
def media_rejected_admin_view(request: HttpRequest) -> HttpResponse:
    """Show quarantined uploads that were rejected by an administrator."""
    limit = max(1, min(_int(request.GET.get("limit"), 100), 200))
    items: list[dict[str, Any]] = []

    for rejected_id in _list_rejected_ids(limit=limit):
        meta_key = f"{REJECTED_UPLOAD_META_PREFIX}/{rejected_id}.json"
        meta = _read_json_from_storage(meta_key) or {}
        storage_key = str(meta.get("rejected_storage_key") or meta.get("storage_key") or "").strip()
        try:
            storage_key = _normalize_storage_key(storage_key) if storage_key else ""
            file_exists = bool(storage_key and default_storage.exists(storage_key))
        except Exception:
            file_exists = False

        items.append(
            {
                "rejected_id": rejected_id,
                "storage_key": storage_key,
                "orig_name": meta.get("orig_name") or "",
                "mime": meta.get("mime") or "",
                "size": meta.get("size") or 0,
                "actor_key": meta.get("actor_key") or "",
                "rejected_at": meta.get("rejected_at"),
                "reject_reason": meta.get("reject_reason") or "사유 없음",
                "reject_mode": meta.get("reject_mode") or "reject_only",
                "restriction": meta.get("restrict") or {},
                "file_exists": file_exists,
                "preview_url": _rejected_preview_url(request, rejected_id) if file_exists else "",
            }
        )

    return render(
        request,
        "ragadmin/media_rejected_admin.html",
        {
            "limit": limit,
            "count": len(items),
            "items": items,
            "delete_url": reverse("api_media_rejected_delete"),
            "pending_url": reverse("ragadmin_media_pending"),
            "admin_upload_url": reverse("ragadmin_media_upload"),
        },
    )


@require_POST
@csrf_protect
@staff_member_required
def api_media_rejected_delete(request: HttpRequest) -> JsonResponse:
    """Permanently remove one quarantined file and all of its review records."""
    try:
        payload = json.loads((request.body or b"{}").decode("utf-8")) if "application/json" in (request.content_type or "") else request.POST
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}

    rejected_id = _safe_review_id(payload.get("rejected_id", ""))
    if not rejected_id:
        return JsonResponse({"ok": False, "error": "invalid_rejected_id"}, status=400)

    meta_key = f"{REJECTED_UPLOAD_META_PREFIX}/{rejected_id}.json"
    meta = _read_json_from_storage(meta_key) or {}
    if not meta:
        return JsonResponse({"ok": False, "error": "rejected_meta_not_found"}, status=404)

    storage_key = str(meta.get("rejected_storage_key") or meta.get("storage_key") or "").strip()
    try:
        storage_key = _normalize_storage_key(storage_key) if storage_key else ""
    except Exception:
        return JsonResponse({"ok": False, "error": "invalid_storage_key"}, status=400)

    # Never let this endpoint delete approved/public media.
    if storage_key and not storage_key.startswith(("rejected/", "pending/")):
        return JsonResponse({"ok": False, "error": "unsafe_storage_key"}, status=400)

    file_deleted = False
    if storage_key and default_storage.exists(storage_key):
        default_storage.delete(storage_key)
        file_deleted = True

    record_deleted = 0
    try:
        from ragapp.moderation_models import ImageUploadRequest

        record_deleted, _ = ImageUploadRequest.objects.filter(storage_key=storage_key).delete()
    except Exception:
        pass

    if default_storage.exists(meta_key):
        default_storage.delete(meta_key)

    return JsonResponse(
        {
            "ok": True,
            "rejected_id": rejected_id,
            "file_deleted": file_deleted,
            "record_deleted": bool(record_deleted),
        }
    )


# ────────────────────────────────────────────────
# (D-2) 승인된 이미지 목록(검색에 잡히는 것들) + 제거
# ────────────────────────────────────────────────
@never_cache
@staff_member_required
@require_GET
def media_approved_admin_view(request: HttpRequest) -> HttpResponse:
    """승인되어 실제로 이미지 검색에 노출되는 이미지 목록."""
    limit = max(1, min(_int(request.GET.get("limit"), 100), 200))
    deleted_ids = _load_deleted_image_ids()
    items: list[dict[str, Any]] = []

    for approved_id in _list_approved_ids(limit=limit):
        meta_key = f"{APPROVED_UPLOAD_META_PREFIX}/{approved_id}.json"
        meta = _read_json_from_storage(meta_key) or {}
        storage_key = str(meta.get("approved_storage_key") or meta.get("storage_key") or "").strip()
        try:
            storage_key = _normalize_storage_key(storage_key) if storage_key else ""
            file_exists = bool(storage_key and default_storage.exists(storage_key))
        except Exception:
            file_exists = False

        pid = str(meta.get("approved_pid") or "").strip()

        items.append(
            {
                "approved_id": approved_id,
                "pid": pid,
                "storage_key": storage_key,
                "orig_name": meta.get("orig_name") or "",
                "caption": meta.get("caption") or "",
                "tags": meta.get("tags") or "",
                "mime": meta.get("mime") or "",
                "size": meta.get("size") or 0,
                "approved_at": meta.get("approved_at"),
                "file_exists": file_exists,
                "removed_from_search": bool(pid and pid in deleted_ids),
                "preview_url": _approved_preview_url(request, storage_key) if file_exists else "",
            }
        )

    return render(
        request,
        "ragadmin/media_approved_admin.html",
        {
            "limit": limit,
            "count": len(items),
            "items": items,
            "remove_url": reverse("api_media_approved_remove"),
            "pending_url": reverse("ragadmin_media_pending"),
            "rejected_url": reverse("ragadmin_media_rejected"),
            "admin_upload_url": reverse("ragadmin_media_upload"),
        },
    )


@require_POST
@csrf_protect
@staff_member_required
def api_media_approved_remove(request: HttpRequest) -> JsonResponse:
    """승인된 이미지를 검색 결과에서 제거한다(기존 미디어 검색이 이미 참조하는
    삭제 id 블록리스트에 추가하는 방식 — media_search_view/media_index_view와
    동일한 매커니즘). 파일/승인 기록 자체는 감사를 위해 남긴다."""
    try:
        payload = json.loads((request.body or b"{}").decode("utf-8")) if "application/json" in (request.content_type or "") else request.POST
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}

    approved_id = _safe_review_id(payload.get("approved_id", ""))
    if not approved_id:
        return JsonResponse({"ok": False, "error": "invalid_approved_id"}, status=400)

    meta_key = f"{APPROVED_UPLOAD_META_PREFIX}/{approved_id}.json"
    meta = _read_json_from_storage(meta_key) or {}
    if not meta:
        return JsonResponse({"ok": False, "error": "approved_meta_not_found"}, status=404)

    pid = str(meta.get("approved_pid") or "").strip()
    if not pid:
        return JsonResponse({"ok": False, "error": "missing_pid"}, status=400)

    _delete_image_from_index(pid)

    return JsonResponse({"ok": True, "approved_id": approved_id, "pid": pid})


# ────────────────────────────────────────────────
# (E) 선처(제재 해제) 운영 페이지 + API
# ────────────────────────────────────────────────
@never_cache
@staff_member_required
@require_GET
def media_penalties_admin_view(request: HttpRequest) -> HttpResponse:
    actor = (request.GET.get("actor_key") or "").strip()
    return render(
        request,
        "ragadmin/media_penalties_admin.html",
        {
            "actor_key": actor,
            "list_url": reverse("api_user_penalty_list"),
            "lift_url": reverse("api_user_penalty_lift"),
            "pending_url": reverse("ragadmin_media_pending"),
            "admin_upload_url": reverse("ragadmin_media_upload"),
        },
    )


def _model_has_field(Model: Any, name: str) -> bool:
    try:
        return any(getattr(f, "name", None) == name for f in Model._meta.get_fields())
    except Exception:
        return False


@require_GET
@staff_member_required
def api_media_penalties_list(request: HttpRequest) -> JsonResponse:
    actor_key = (request.GET.get("actor_key") or "").strip()

    try:
        from ragapp.moderation_models import UserPenalty
    except Exception:
        return JsonResponse(
            {"ok": True, "items": [], "warning": "UserPenalty model not available"},
            status=200,
            json_dumps_params={"ensure_ascii": False},
        )

    now = timezone.now()

    qs = UserPenalty.objects.all().order_by("-pk")
    if actor_key:
        qs = qs.filter(actor_key=actor_key)

    Mode = getattr(UserPenalty, "Mode", None)
    mode_permaban = getattr(Mode, "PERMABAN", "PERMABAN") if Mode else "PERMABAN"
    mode_suspended = getattr(Mode, "SUSPENDED", "SUSPENDED") if Mode else "SUSPENDED"

    items: List[Dict[str, Any]] = []
    for obj in qs[:500]:
        mode = getattr(obj, "mode", "") or ""
        suspended_until = getattr(obj, "suspended_until", None)

        # 활성 제재만 노출: 영구제한이거나, 정지 기간이 아직 안 끝났을 때만
        if mode == mode_permaban:
            is_active = True
        elif mode == mode_suspended and suspended_until:
            is_active = suspended_until > now
        else:
            is_active = False

        if not is_active:
            continue

        if mode == mode_permaban:
            kind_label = "영구 제한"
            until_iso = "영구"
        else:
            kind_label = "정지"
            until_iso = suspended_until.isoformat() if suspended_until else None

        created_at = getattr(obj, "created_at", None)
        created_iso = created_at.isoformat() if created_at else None

        items.append(
            {
                "id": getattr(obj, "pk", None),
                "actor_key": getattr(obj, "actor_key", ""),
                "kind": kind_label,
                "reason": getattr(obj, "reason", "") or "",
                "until": until_iso,
                "created_at": created_iso,
            }
        )

    return JsonResponse({"ok": True, "items": items}, status=200, json_dumps_params={"ensure_ascii": False})


@require_POST
@csrf_protect
@staff_member_required
def api_media_penalties_lift(request: HttpRequest) -> JsonResponse:
    actor_key = ""
    reason = ""
    try:
        if request.content_type and "application/json" in request.content_type.lower():
            raw = (request.body or b"").decode("utf-8") or ""
            payload = json.loads(raw) if raw else {}
            actor_key = str(payload.get("actor_key") or "").strip()
            reason = str(payload.get("reason") or "").strip()
        else:
            actor_key = str(request.POST.get("actor_key") or "").strip()
            reason = str(request.POST.get("reason") or "").strip()
    except Exception:
        actor_key = str(request.POST.get("actor_key") or "").strip()

    if not actor_key:
        return JsonResponse({"ok": False, "error": "actor_key_required"}, status=400, json_dumps_params={"ensure_ascii": False})

    try:
        from ragapp.moderation_models import UserPenalty
    except Exception:
        return JsonResponse({"ok": False, "error": "UserPenalty model not available"}, status=500, json_dumps_params={"ensure_ascii": False})

    reviewer = getattr(request, "user", None)

    existed = UserPenalty.objects.filter(actor_key=actor_key).exists()
    if not existed:
        return JsonResponse({"ok": True, "actor_key": actor_key, "lifted": 0}, status=200, json_dumps_params={"ensure_ascii": False})

    try:
        UserPenalty.pardon(actor_key, by_user=reviewer)
    except Exception:
        log.exception("penalty pardon failed actor_key=%s", actor_key)
        return JsonResponse({"ok": False, "error": "pardon_failed"}, status=500, json_dumps_params={"ensure_ascii": False})

    return JsonResponse({"ok": True, "actor_key": actor_key, "lifted": 1}, status=200, json_dumps_params={"ensure_ascii": False})


# ---- aliases (urls/템플릿 호환용) ----
api_user_penalty_lift = api_media_penalties_lift
api_user_penalty_list = api_media_penalties_list
