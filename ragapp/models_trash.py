# ragapp/models_trash.py
from __future__ import annotations

from django.db import models
from django.utils import timezone


class TrashedRecord(models.Model):
    """
    어드민에서 삭제한 레코드를 즉시 지우지 않고 스냅샷으로 보관하는 휴지통.

    - Django admin의 ModelAdmin.delete_model/delete_queryset을 가로채서
      실제 delete() 전에 여기에 스냅샷을 남긴다(ragapp/services/trash_service.py).
    - 파일 필드(FileField/ImageField)를 가진 모델(MediaAsset, TableDataset 등)은
      실제 파일도 trash/ 경로로 옮겨서 함께 보존한다(file_trash_map).
    - "복구"는 스냅샷으로 원래 레코드(+파일)를 되살리고,
      "영구 삭제"는 보관 중인 파일/스냅샷 본문을 지우고 감사 로그만 남긴다.
    """

    model_label = models.CharField(
        max_length=200, db_index=True, help_text="app_label.model_name (예: ragapp.mediaasset)"
    )
    object_pk = models.CharField(max_length=64, db_index=True)
    object_repr = models.CharField(max_length=500, blank=True, default="")

    data_json = models.JSONField(
        default=dict, blank=True, help_text="삭제 당시 필드 스냅샷(복구용, django serializers 포맷)"
    )
    file_trash_map = models.JSONField(
        default=dict, blank=True, help_text="{필드명: 격리보관된 storage key} — 파일이 있는 모델용"
    )

    deleted_at = models.DateTimeField(default=timezone.now, db_index=True)
    deleted_by = models.CharField(max_length=150, blank=True, default="")

    restored = models.BooleanField(default=False, db_index=True)
    restored_at = models.DateTimeField(null=True, blank=True)

    purged = models.BooleanField(default=False, db_index=True)
    purged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-deleted_at", "-id"]
        verbose_name = "휴지통 항목"
        verbose_name_plural = "휴지통"
        indexes = [
            models.Index(fields=["model_label", "deleted_at"]),
            models.Index(fields=["restored", "purged", "deleted_at"]),
        ]

    def __str__(self) -> str:
        ts = timezone.localtime(self.deleted_at).strftime("%Y-%m-%d %H:%M")
        return f"[{self.model_label}] {self.object_repr or self.object_pk} @ {ts}"

    @property
    def status(self) -> str:
        if self.purged:
            return "purged"
        if self.restored:
            return "restored"
        return "trashed"
