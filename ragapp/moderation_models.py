# ragapp/moderation_models.py
from __future__ import annotations

from datetime import timedelta
from django.conf import settings
from django.db import models
from django.utils import timezone


class UserPenalty(models.Model):
    class Mode(models.TextChoices):
        NONE = "NONE", "None"
        SUSPENDED = "SUSPENDED", "Suspended"
        PERMABAN = "PERMABAN", "Permaban"

    actor_key = models.CharField(max_length=80, unique=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="penalties"
    )

    mode = models.CharField(max_length=16, choices=Mode.choices, default=Mode.NONE, db_index=True)
    suspended_until = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True, default="")

    last_ip = models.CharField(max_length=64, blank=True, default="")
    last_ua = models.CharField(max_length=256, blank=True, default="")

    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="penalties_updated"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def is_blocked(self) -> bool:
        if self.mode == self.Mode.PERMABAN:
            return True
        if self.mode == self.Mode.SUSPENDED and self.suspended_until:
            return self.suspended_until > timezone.now()
        return False

    def remaining_seconds(self) -> int:
        if self.mode != self.Mode.SUSPENDED or not self.suspended_until:
            return 0
        delta = self.suspended_until - timezone.now()
        return max(0, int(delta.total_seconds()))

    @classmethod
    def apply_suspend(cls, actor_key: str, days: int, *, by_user=None, reason: str = "") -> "UserPenalty":
        now = timezone.now()
        p, _ = cls.objects.get_or_create(actor_key=actor_key, defaults={"mode": cls.Mode.NONE})

        base = now
        if p.mode == cls.Mode.SUSPENDED and p.suspended_until and p.suspended_until > now:
            base = p.suspended_until  # 남아있으면 연장

        p.mode = cls.Mode.SUSPENDED
        p.suspended_until = base + timedelta(days=int(days))
        p.reason = reason or p.reason
        p.updated_by = by_user if by_user is not None else p.updated_by
        p.save()
        return p

    @classmethod
    def apply_permaban(cls, actor_key: str, *, by_user=None, reason: str = "") -> "UserPenalty":
        p, _ = cls.objects.get_or_create(actor_key=actor_key, defaults={"mode": cls.Mode.NONE})
        p.mode = cls.Mode.PERMABAN
        p.suspended_until = None
        p.reason = reason or p.reason
        p.updated_by = by_user if by_user is not None else p.updated_by
        p.save()
        return p

    @classmethod
    def pardon(cls, actor_key: str, *, by_user=None) -> "UserPenalty":
        p, _ = cls.objects.get_or_create(actor_key=actor_key, defaults={"mode": cls.Mode.NONE})
        p.mode = cls.Mode.NONE
        p.suspended_until = None
        p.reason = ""
        p.updated_by = by_user if by_user is not None else p.updated_by
        p.save()
        return p


class ImageUploadRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    storage_key = models.CharField(max_length=512, unique=True, db_index=True)
    actor_key = models.CharField(max_length=80, db_index=True)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="image_upload_requests"
    )

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    reject_reason = models.TextField(blank=True, default="")

    # 업로드 메타(심사/승인 시 재사용)
    orig_name = models.CharField(max_length=255, blank=True, default="")
    mime = models.CharField(max_length=64, blank=True, default="")
    size = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True, default="")
    sha16 = models.CharField(max_length=16, blank=True, default="", db_index=True)

    caption = models.TextField(blank=True, default="")
    tags = models.TextField(blank=True, default="")
    ai_meta = models.JSONField(default=dict, blank=True)

    # 승인 후 인덱싱 결과(있으면 저장)
    chroma_pid = models.CharField(max_length=128, blank=True, default="", db_index=True)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="image_upload_requests_reviewed"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
