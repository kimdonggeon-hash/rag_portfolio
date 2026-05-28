from __future__ import annotations

from typing import Iterable, Optional
from django.utils import timezone

try:
    from ragapp.models import BoardAdminActionLog  # ✅ 네 프로젝트 모델
except Exception:  # pragma: no cover
    BoardAdminActionLog = None  # type: ignore


def _fields(Model) -> set[str]:
    try:
        return {f.name for f in Model._meta.get_fields() if getattr(f, "concrete", False)}
    except Exception:
        return set()


def write_board_admin_action_log(
    *,
    request,
    action: str,
    done: int = 0,
    status: str = "",
    note: str = "",
    report_ids: Optional[Iterable[int]] = None,
    auto_only: bool = False,
    filter_qs: str = "",
) -> None:
    """
    BoardAdminActionLog에 '있는 필드만' 채워서 저장.
    실패해도 운영 기능(신고 처리 등)은 계속 진행되게 조용히 무시.
    """
    if BoardAdminActionLog is None:
        return

    try:
        Model = BoardAdminActionLog
        f = _fields(Model)
        it = Model()

        if "action" in f:
            it.action = action

        if "done" in f:
            it.done = int(done or 0)

        if "status" in f and status:
            it.status = status

        if "auto_only" in f:
            it.auto_only = bool(auto_only)

        # actor / actor_name
        user = getattr(request, "user", None)
        if user and getattr(user, "is_authenticated", False):
            if "actor" in f:
                it.actor = user
            if "actor_name" in f:
                it.actor_name = getattr(user, "username", "") or "staff"
        else:
            if "actor_name" in f:
                it.actor_name = "staff"

        # request meta
        if "path" in f:
            it.path = getattr(request, "path", "") or ""

        if "query_string" in f:
            it.query_string = (getattr(request, "META", {}) or {}).get("QUERY_STRING", "")

        if "filter_qs" in f and filter_qs:
            it.filter_qs = filter_qs

        if "note" in f and note:
            it.note = note

        # report_ids 저장 형태: "1,2,3"
        if "report_ids" in f and report_ids:
            it.report_ids = ",".join(str(int(x)) for x in report_ids)

        if "created_at" in f and not getattr(it, "created_at", None):
            it.created_at = timezone.now()

        it.save()
    except Exception:
        return
