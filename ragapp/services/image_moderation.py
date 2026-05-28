# ragapp/services/image_moderation.py
from __future__ import annotations

import mimetypes
import os
import uuid
from pathlib import Path
from typing import Optional, Tuple

from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile

from ragapp.moderation_models import UserPenalty, ImageUploadRequest

from ragapp.services.vertex_embed import embed_image_file
from ragapp.services.chroma_media import add_image_item


# ────────────────────────────────────────────────
# actor / request helpers
# ────────────────────────────────────────────────
def get_remote_ip(request) -> str:
    xff = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    return xff or (request.META.get("REMOTE_ADDR") or "")


def get_user_agent(request) -> str:
    return (request.META.get("HTTP_USER_AGENT") or "")[:256]


def _anon_actor_fingerprint(request) -> str:
    """
    세션/DB에 의존하지 않는 익명 식별자.
    - raw IP를 그대로 쓰지 않고(정책/프라이버시), SECRET_KEY 기반 HMAC으로 익명화.
    - sess.save() 같은 DB 세션 생성은 절대 하지 않음.
    """
    ip = (get_remote_ip(request) or "").strip()
    ua = (get_user_agent(request) or "").strip()
    raw = f"{ip}|{ua}"
    # Django SECRET_KEY 사용
    digest = salted_hmac("ragapp.actor", raw).hexdigest()[:20]
    return digest


def get_actor_key(request) -> str:
    """
    ✅ DB 세션 생성 금지(= sess.save() 금지)
    - 인증 사용자: user:pk
    - 익명: 세션키가 이미 있으면 sess:<key> (저장/생성 안 함)
    - 세션키가 없으면 anon:<hmac> 로 폴백 (DB/세션 저장 안 함)
    """
    u = getattr(request, "user", None)
    if u and getattr(u, "is_authenticated", False):
        return f"user:{u.pk}"

    sess = getattr(request, "session", None)
    if sess is not None:
        # session_key는 쿠키가 이미 있으면 존재할 수 있음(생성/저장 필요 없음)
        sk = getattr(sess, "session_key", None)
        if sk:
            return f"sess:{sk}"

    # SessionMiddleware가 없거나, session_key가 없으면 익명 폴백
    return f"anon:{_anon_actor_fingerprint(request)}"


# ────────────────────────────────────────────────
# penalty
# ────────────────────────────────────────────────
def get_penalty(actor_key: str) -> Optional[UserPenalty]:
    try:
        return UserPenalty.objects.filter(actor_key=actor_key).first()
    except Exception:
        # DB가 일시 장애/연결 포화여도 서비스가 죽지 않도록 fail-open(제재 미적용) 처리
        return None


def is_actor_blocked(actor_key: str) -> Tuple[bool, str]:
    p = get_penalty(actor_key)
    if not p:
        return False, ""

    if p.mode == UserPenalty.Mode.PERMABAN:
        return True, "이용이 제한된 상태입니다."

    if (
        p.mode == UserPenalty.Mode.SUSPENDED
        and p.suspended_until
        and p.suspended_until > timezone.now()
    ):
        try:
            secs = int(p.remaining_seconds())
        except Exception:
            secs = int((p.suspended_until - timezone.now()).total_seconds())
        hrs = max(0, secs // 3600)
        return True, f"이용이 제한된 상태입니다. (남은 시간 약 {hrs}시간)"

    return False, ""


# ────────────────────────────────────────────────
# storage_key normalize / guards
# ────────────────────────────────────────────────
def _normalize_storage_key(key: str) -> str:
    s = (key or "").strip().replace("\\", "/").lstrip("/")
    # /uploads/... 로 들어오면 uploads/ 제거 (네 프로젝트 규칙에 맞춘 보수적 정규화)
    if s.startswith("uploads/"):
        s = s[len("uploads/") :]
    # .. 경로 방지
    if ".." in Path(s).parts:
        return ""
    return s


def _must_be_pending_key(key: str) -> None:
    # 너 정책이 "보류 업로드는 pending/ 아래"라면 이 가드가 가장 안전함
    if not key.startswith("pending/"):
        raise ValueError("storage_key_not_pending")


def is_public_image(storage_key: str) -> bool:
    """
    ✅ 예외 시 공개(True)로 풀어버리면 보안 사고가 날 수 있어서 fail-closed(False) 권장.
    """
    try:
        key = _normalize_storage_key(storage_key)
        if not key:
            return False

        obj = ImageUploadRequest.objects.filter(storage_key=key).first()
        if obj:
            return obj.status == ImageUploadRequest.Status.APPROVED

        # ✅ pending은 레코드가 없어도 기본 비공개
        if key.startswith("pending/"):
            return False

        # 기존 데이터 호환 (images/ 아래 등)
        return True
    except Exception:
        # 🔒 안전하게 비공개 처리
        return False


def create_pending_upload(
    *,
    storage_key: str,
    actor_key: str,
    user=None,
    orig_name: str = "",
    mime: str = "",
    size: int = 0,
    sha256: str = "",
    caption: str = "",
    tags: str = "",
    ai_meta: dict | None = None,
) -> ImageUploadRequest:
    ai_meta = ai_meta or {}
    key = _normalize_storage_key(storage_key)

    obj, _ = ImageUploadRequest.objects.update_or_create(
        storage_key=key,
        defaults={
            "actor_key": actor_key,
            "user": user if (user is not None and getattr(user, "is_authenticated", False)) else None,
            "status": ImageUploadRequest.Status.PENDING,
            "orig_name": orig_name or "",
            "mime": mime or "",
            "size": int(size or 0),
            "sha256": sha256 or "",
            "sha16": (sha256 or "")[:16],
            "caption": caption or "",
            "tags": tags or "",
            "ai_meta": ai_meta or {},
        },
    )
    return obj


def _tmp_dir() -> Path:
    base = os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp"
    p = Path(base).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    d = p / "ragapp_mod_tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ────────────────────────────────────────────────
# approve / reject
# ────────────────────────────────────────────────
@transaction.atomic
def approve_and_index(*, storage_key: str, reviewer) -> ImageUploadRequest:
    key = _normalize_storage_key(storage_key)
    if not key:
        raise ValueError("invalid_storage_key")
    _must_be_pending_key(key)

    obj = ImageUploadRequest.objects.select_for_update().get(storage_key=key)
    if obj.status != ImageUploadRequest.Status.PENDING:
        return obj

    # ✅ pending/ 제거해서 images/... 로 승격 (너 pending 구조가 pending/images/... 라면 딱 맞음)
    dest_key = key[len("pending/") :]
    if not dest_key or dest_key.startswith("meta/"):
        raise ValueError("invalid_dest_key")

    tmp = _tmp_dir() / f"approve_{uuid.uuid4().hex}_{Path(dest_key).name}"
    try:
        with default_storage.open(key, "rb") as rf:
            data = rf.read()

        # dest 먼저 저장(동일명 충돌 대비)
        try:
            if default_storage.exists(dest_key):
                default_storage.delete(dest_key)
        except Exception:
            pass
        dest_key_saved = default_storage.save(dest_key, ContentFile(data))
        dest_key_saved = _normalize_storage_key(dest_key_saved) or dest_key

        # pending 삭제(선택: 승인되면 pending는 보통 제거)
        try:
            if default_storage.exists(key):
                default_storage.delete(key)
        except Exception:
            pass

        tmp.write_bytes(data)

        mime = (obj.mime or "").strip().lower() or (mimetypes.guess_type(dest_key_saved)[0] or "image/jpeg")
        vec = embed_image_file(str(tmp), mime=mime)

        extra_meta = {
            "orig_name": obj.orig_name,
            "actor_key": obj.actor_key,
            "approval": "approved",
            "approved_at": timezone.now().isoformat(),
            "pending_storage_key": key,
            "approved_storage_key": dest_key_saved,
        }

        pid = add_image_item(
            path=str(dest_key_saved),  # ✅ 승인된 키로 인덱싱
            embedding=vec,
            caption=(obj.caption or ""),
            original_name=(obj.orig_name or ""),
            tags=(obj.tags or ""),
            extra_meta=extra_meta,
        )

        # ✅ DB도 승인된 키로 갱신
        obj.storage_key = dest_key_saved
        obj.status = ImageUploadRequest.Status.APPROVED
        obj.reviewed_by = reviewer
        obj.reviewed_at = timezone.now()
        obj.chroma_pid = str(pid or "")

        # ai_meta에 흔적 남기고 싶으면:
        try:
            meta = dict(obj.ai_meta or {})
            meta["pending_storage_key"] = key
            meta["approved_storage_key"] = dest_key_saved
            obj.ai_meta = meta
        except Exception:
            pass

        obj.save(
            update_fields=[
                "storage_key",
                "status",
                "reviewed_by",
                "reviewed_at",
                "chroma_pid",
                "ai_meta",
                "updated_at",
            ]
        )
        return obj

    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


@transaction.atomic
def reject_upload(
    *,
    storage_key: str,
    reviewer,
    reason: str = "",
    suspend_days: int | None = None,
    permaban: bool = False,
    delete_blob: bool = False,
) -> ImageUploadRequest:
    key = _normalize_storage_key(storage_key)
    if not key:
        raise ValueError("invalid_storage_key")

    # ✅ reject도 pending만 (실수 방지)
    _must_be_pending_key(key)

    obj = ImageUploadRequest.objects.select_for_update().get(storage_key=key)

    if obj.status != ImageUploadRequest.Status.PENDING:
        return obj

    obj.status = ImageUploadRequest.Status.REJECTED
    obj.reject_reason = (reason or "").strip()
    obj.reviewed_by = reviewer
    obj.reviewed_at = timezone.now()
    obj.save(update_fields=["status", "reject_reason", "reviewed_by", "reviewed_at", "updated_at"])

    actor_key = obj.actor_key
    if permaban:
        UserPenalty.apply_permaban(actor_key, by_user=reviewer, reason=reason or "영구 제한")
    elif suspend_days is not None and int(suspend_days) > 0:
        UserPenalty.apply_suspend(actor_key, days=int(suspend_days), by_user=reviewer, reason=reason or "기간 제한")

    # ✅ 오브젝트 삭제는 pending만 수행
    if delete_blob:
        try:
            if default_storage.exists(key):
                default_storage.delete(key)
        except Exception:
            pass

    return obj
