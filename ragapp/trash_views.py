# ragapp/trash_views.py
from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from ragapp.models_trash import TrashedRecord
from ragapp.services.trash_service import purge_trashed_record, restore_trashed_record

log = logging.getLogger(__name__)


@staff_member_required
def trash_admin_view(request: HttpRequest) -> HttpResponse:
    tab = (request.GET.get("tab") or "active").strip()
    model_filter = (request.GET.get("model") or "").strip()

    qs = TrashedRecord.objects.all()
    if tab == "purged":
        qs = qs.filter(purged=True)
    elif tab == "restored":
        qs = qs.filter(restored=True)
    else:
        tab = "active"
        qs = qs.filter(purged=False, restored=False)

    if model_filter:
        qs = qs.filter(model_label=model_filter)

    model_choices = (
        TrashedRecord.objects.filter(purged=False, restored=False)
        .order_by("model_label")
        .values_list("model_label", flat=True)
        .distinct()
    )

    ctx = {
        "title": "휴지통",
        "tab": tab,
        "model_filter": model_filter,
        "model_choices": sorted(set(model_choices)),
        "records": qs[:500],
        "active_count": TrashedRecord.objects.filter(purged=False, restored=False).count(),
    }
    return render(request, "ragadmin/trash_admin.html", ctx)


@staff_member_required
@csrf_protect
@require_POST
def trash_restore_view(request: HttpRequest, record_id: int) -> HttpResponse:
    record = get_object_or_404(TrashedRecord, pk=record_id)
    try:
        restore_trashed_record(record)
        messages.success(request, f"복구했습니다: {record.object_repr or record.object_pk}")
    except Exception as e:
        log.exception("trash restore failed record_id=%s", record_id)
        messages.error(request, f"복구 실패: {e}")

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": True})
    return redirect(reverse("ragadmin:trash"))


@staff_member_required
@csrf_protect
@require_POST
def trash_purge_view(request: HttpRequest, record_id: int) -> HttpResponse:
    record = get_object_or_404(TrashedRecord, pk=record_id)
    try:
        purge_trashed_record(record)
        messages.success(request, f"영구 삭제했습니다: {record.object_repr or record.object_pk}")
    except Exception as e:
        log.exception("trash purge failed record_id=%s", record_id)
        messages.error(request, f"영구 삭제 실패: {e}")

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": True})
    return redirect(reverse("ragadmin:trash"))
