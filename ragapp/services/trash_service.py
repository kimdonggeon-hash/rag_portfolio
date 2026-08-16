# ragapp/services/trash_service.py
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any, Iterable

from django.apps import apps
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.serializers import deserialize, serialize
from django.db.models.fields.files import FileField
from django.utils import timezone

log = logging.getLogger(__name__)

TRASH_STORAGE_PREFIX = "trash"


def _model_label(obj) -> str:
    return f"{obj._meta.app_label}.{obj._meta.model_name}"


def _file_fields(obj) -> list[FileField]:
    return [f for f in obj._meta.get_fields() if isinstance(f, FileField)]


def _quarantine_files(obj) -> dict[str, str]:
    """
    obj가 가진 FileField(=실제 업로드 파일)들을 trash/ 아래로 옮기고,
    {필드명: 옮겨진 storage key} 매핑을 돌려준다.
    실패한 필드는 매핑에서 빠지고(=원본 자리에 그대로 남음) 로그만 남긴다.
    """
    moved: dict[str, str] = {}
    for field in _file_fields(obj):
        file_ = getattr(obj, field.name, None)
        name = getattr(file_, "name", "") or ""
        if not name:
            continue
        try:
            if not default_storage.exists(name):
                continue
            with default_storage.open(name, "rb") as fh:
                data = fh.read()

            label = _model_label(obj).replace(".", "_")
            dest = f"{TRASH_STORAGE_PREFIX}/{label}/{uuid.uuid4().hex[:12]}_{Path(name).name}"
            default_storage.save(dest, ContentFile(data))

            try:
                default_storage.delete(name)
            except Exception:
                log.warning("trash: original file delete failed (kept as-is): %s", name, exc_info=True)

            moved[field.name] = dest
        except Exception:
            log.exception("trash: quarantine failed for %s.%s (name=%s)", _model_label(obj), field.name, name)
    return moved


def move_to_trash(objs: Iterable[Any], *, actor: str = "") -> list["TrashedRecord"]:  # noqa: F821
    from ragapp.models_trash import TrashedRecord

    created: list[TrashedRecord] = []
    for obj in objs:
        if isinstance(obj, TrashedRecord):
            # 휴지통 항목 자체를 관리자에서 지우는 경우(있다면)는 그냥 진짜로 지운다.
            obj.delete()
            continue
        try:
            snapshot = json.loads(serialize("json", [obj]))[0]
        except Exception:
            log.exception("trash: serialize failed for %s pk=%s", _model_label(obj), obj.pk)
            # 스냅샷을 못 만들면 휴지통에 못 담으므로, 이 객체는 그냥 평소처럼(즉시) 삭제되게 둔다.
            obj.delete()
            continue

        file_map = _quarantine_files(obj)

        record = TrashedRecord.objects.create(
            model_label=_model_label(obj),
            object_pk=str(obj.pk),
            object_repr=str(obj)[:500],
            data_json=snapshot,
            file_trash_map=file_map,
            deleted_at=timezone.now(),
            deleted_by=actor or "",
        )
        created.append(record)

        obj.delete()

    return created


def restore_trashed_record(record: "TrashedRecord") -> Any:  # noqa: F821
    if record.purged:
        raise RuntimeError("영구 삭제된 항목은 복구할 수 없습니다.")
    if record.restored:
        raise RuntimeError("이미 복구된 항목입니다.")

    app_label, model_name = record.model_label.split(".", 1)
    model = apps.get_model(app_label, model_name)

    data = dict(record.data_json or {})
    fields = dict(data.get("fields") or {})

    # 파일 필드 복구: trash 위치 -> 원래 위치(비어있으면). 자리가 차 있으면 trash 위치 그대로 사용.
    for field_name, trash_key in (record.file_trash_map or {}).items():
        if not default_storage.exists(trash_key):
            continue
        original_key = fields.get(field_name) or ""
        target_key = trash_key
        if original_key and not default_storage.exists(original_key):
            try:
                with default_storage.open(trash_key, "rb") as fh:
                    payload = fh.read()
                default_storage.save(original_key, ContentFile(payload))
                default_storage.delete(trash_key)
                target_key = original_key
            except Exception:
                log.exception("trash: restore file move failed, keeping trash copy: %s", trash_key)
                target_key = trash_key
        fields[field_name] = target_key

    data["fields"] = fields

    restored_obj = None
    for deserialized in deserialize("json", json.dumps([data])):
        deserialized.save()
        restored_obj = deserialized.object

    record.restored = True
    record.restored_at = timezone.now()
    record.file_trash_map = {}
    record.save(update_fields=["restored", "restored_at", "file_trash_map"])

    return restored_obj


def purge_trashed_record(record: "TrashedRecord") -> None:  # noqa: F821
    if record.purged:
        return
    if record.restored:
        raise RuntimeError("이미 복구된 항목은 영구 삭제할 수 없습니다(복구된 실제 레코드를 지워주세요).")

    for _field_name, trash_key in (record.file_trash_map or {}).items():
        try:
            if default_storage.exists(trash_key):
                default_storage.delete(trash_key)
        except Exception:
            log.exception("trash: purge file delete failed: %s", trash_key)

    record.purged = True
    record.purged_at = timezone.now()
    record.data_json = {}
    record.file_trash_map = {}
    record.save(update_fields=["purged", "purged_at", "data_json", "file_trash_map"])


def purge_expired_trash(*, retention_days: int | None = None) -> int:
    """
    보관 기간이 지난(=아직 복구/영구삭제 되지 않은) 휴지통 항목을 자동으로
    영구 삭제한다. retention_days를 안 주면 TrashSettings(관리자가 화면에서
    고른 값)을 사용한다. 0(또는 None)이면 아무 것도 하지 않는다.

    반환: 실제로 영구 삭제한 항목 수.
    """
    from ragapp.models_trash import TrashedRecord, TrashSettings

    if retention_days is None:
        retention_days = TrashSettings.get_solo().retention_days

    if not retention_days or retention_days <= 0:
        return 0

    cutoff = timezone.now() - timezone.timedelta(days=retention_days)
    qs = TrashedRecord.objects.filter(purged=False, restored=False, deleted_at__lt=cutoff)

    purged_count = 0
    for record in qs.iterator():
        try:
            purge_trashed_record(record)
            purged_count += 1
        except Exception:
            log.exception("trash: auto-purge failed for record id=%s", record.id)

    return purged_count
