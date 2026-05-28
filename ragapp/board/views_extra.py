# ragapp/board/views_extra.py
from __future__ import annotations

import re
import time
import logging
from datetime import timedelta
from typing import Any, Dict, Tuple, Optional, List
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.contrib.auth.decorators import login_required
from django.http import Http404, QueryDict, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_GET

from .models import BoardPost, BoardComment, BoardReport, BoardAdminActionLog
from .banlib import set_ban

# ✅ 여기서 import 깨지면 /board/ 전체가 500 될 수 있어서 fallback을 둔다
try:
    from .views import _categories_for_sidebar, _is_staff  # type: ignore
except Exception:
    def _is_staff(u) -> bool:  # type: ignore
        try:
            return bool(u and getattr(u, "is_authenticated", False) and (getattr(u, "is_staff", False) or getattr(u, "is_superuser", False)))
        except Exception:
            return False

    def _categories_for_sidebar(request: HttpRequest):  # type: ignore
        return []


try:
    from .banlib import add_points as _banlib_add_points  # type: ignore
except Exception:  # pragma: no cover
    _banlib_add_points = None  # type: ignore


_FP_RE = re.compile(r"^[0-9a-f]{12}$")
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# small utils
# ─────────────────────────────────────────────────────────────

def _get_int_setting(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default))
    except Exception:
        return default


def _append_qs(url: str, extra: Dict[str, str]) -> str:
    try:
        u = urlsplit(url)
        q = dict(parse_qsl(u.query, keep_blank_values=True))
        q.update({k: str(v) for k, v in extra.items()})
        return urlunsplit((u.scheme, u.netloc, u.path, urlencode(q), u.fragment))
    except Exception:
        return url


def _strip_bulk_params(qs_text: str) -> str:
    """
    bulk_done/bulk_action 같은 배너 파라미터가 필터에 섞이지 않게 제거.
    """
    qd = QueryDict(qs_text or "", mutable=True)
    for k in ["bulk_done", "bulk_action"]:
        if k in qd:
            try:
                qd.pop(k, None)
            except Exception:
                try:
                    del qd[k]
                except Exception:
                    pass
    return qd.urlencode()


def _status_value(name: str, fallback: str) -> str:
    """
    BoardReport.Status(TextChoices/Enum) 와 문자열 모두 안전 대응.
    """
    try:
        st = getattr(BoardReport, "Status", None)
        if st is not None and hasattr(st, name):
            v = getattr(st, name)
            return getattr(v, "value", v)
    except Exception:
        pass
    return fallback


def _report_reason_label(reason: str) -> str:
    m = {
        "spam": "스팸/광고",
        "abuse": "욕설/혐오/폭력",
        "illegal": "불법/위험 정보",
        "privacy": "개인정보 노출",
        "other": "기타",
    }
    return m.get(reason or "", reason or "기타")


def _build_default_note(r: BoardReport, action: str) -> str:
    tgt = "글" if r.target_type == BoardReport.TargetType.POST else "댓글"
    reason = _report_reason_label(r.reason)

    if action == "reject":
        return f"[반려] {tgt} 신고 검토 결과, 정책 위반을 확인하지 못해 반려했습니다. (사유: {reason})"

    if action == "resolve":
        return f"[확인] {tgt} 신고를 확인했습니다. 추가 조치 없이 종결합니다. (사유: {reason})"

    if action == "resolve_hide":
        return f"[조치] {tgt}이(가) 정책 위반 소지가 있어 숨김 처리했습니다. (사유: {reason})"

    if action == "resolve_delete":
        return f"[조치] {tgt}이(가) 정책 위반 소지가 있어 삭제 처리했습니다. (사유: {reason})"

    if action == "resolve_delete_ban":
        return f"[조치] {tgt}이(가) 정책 위반 소지가 있어 삭제 + 즉시 차단 처리했습니다. (사유: {reason})"

    return f"[확인] {tgt} 신고 확인 완료. (사유: {reason})"


def _admin_ip(request: HttpRequest) -> str:
    ip = (
        request.META.get("HTTP_CF_CONNECTING_IP")
        or request.META.get("HTTP_X_FORWARDED_FOR")
        or request.META.get("REMOTE_ADDR")
        or ""
    )
    ip = (ip.split(",")[0] if ip else "").strip()
    return ip[:64]


def _admin_ua(request: HttpRequest) -> str:
    ua = (request.META.get("HTTP_USER_AGENT") or "").strip()
    return ua[:200]


def _filtered_reports_from_qs(qs_text: str):
    """
    report_list 화면의 GET(querystring)과 “완전히 같은” 필터를 재구성.
    """
    qd = QueryDict(qs_text or "", mutable=False)

    status = (qd.get("status") or "open").strip()
    q = (qd.get("q") or "").strip()
    auto_only = (qd.get("auto") or "").strip() == "1"

    qs = (
        BoardReport.objects
        .select_related("post", "comment", "comment__post", "handled_by", "reporter")
        .all()
    )

    if status in ("open", "resolved", "rejected"):
        qs = qs.filter(status=status)

    if auto_only:
        qs = qs.filter(message__startswith="[AUTO]")

    if q:
        qs = qs.filter(
            Q(message__icontains=q) |
            Q(admin_note__icontains=q) |
            Q(reason__icontains=q) |
            Q(reporter_fp__icontains=q)
        )

    return qs.order_by("-created_at")


# ─────────────────────────────────────────────────────────────
# ✅ Admin Log writer (필드 차이로 조용히 실패하는 것 방지)
# ─────────────────────────────────────────────────────────────

def _choice_value(v: Any, fallback: str) -> str:
    try:
        if hasattr(v, "value"):
            return str(v.value)
        return str(v)
    except Exception:
        return fallback


def _field_names(model) -> set[str]:
    try:
        return {f.name for f in model._meta.fields}
    except Exception:
        return set()


def _trim(model, field_name: str, s: str) -> str:
    s = s if isinstance(s, str) else str(s)
    try:
        f = model._meta.get_field(field_name)
        ml = getattr(f, "max_length", None)
        if ml and len(s) > int(ml):
            return s[: int(ml)]
    except Exception:
        pass
    return s


def _pack_report_ids(ids: List[int] | Tuple[int, ...] | str) -> Tuple[str, str, str]:
    """
    returns:
      - csv: "12,33,40"
      - preview: "12, 33, 40"
      - blob: ",12,33,40,"
    """
    if isinstance(ids, str):
        raw = ids.strip()
        parts: List[int] = []
        for x in raw.replace(" ", "").split(","):
            if x.isdigit():
                parts.append(int(x))
        ids_list = parts
    else:
        ids_list = [int(x) for x in ids if int(x) > 0]

    csv = ",".join(str(x) for x in ids_list)
    preview = ", ".join(str(x) for x in ids_list)
    blob = ("," + csv + ",") if csv else ""
    return (csv, preview, blob)


def _adminlog_create(
    request: HttpRequest,
    *,
    action_value: str,
    done: int,
    status: str = "",
    q: str = "",
    auto_only: bool = False,
    report_ids: List[int] | Tuple[int, ...] | str = "",
    note: str = "",
    filter_qs: str = "",
    query_string: str = "",
    path: str = "",
) -> None:
    fields = _field_names(BoardAdminActionLog)
    if not fields:
        return

    u = getattr(request, "user", None)
    actor = u if (u and getattr(u, "is_authenticated", False)) else None

    csv, preview, blob = _pack_report_ids(report_ids)

    kwargs: Dict[str, Any] = {}

    if "actor" in fields:
        kwargs["actor"] = actor
    if "actor_name" in fields:
        kwargs["actor_name"] = _trim(
            BoardAdminActionLog,
            "actor_name",
            getattr(u, "username", "") if actor else "",
        )

    if "action" in fields:
        kwargs["action"] = _trim(BoardAdminActionLog, "action", action_value)

    if "done" in fields:
        kwargs["done"] = int(done)

    if "status" in fields:
        kwargs["status"] = _trim(BoardAdminActionLog, "status", status or "")

    if "q" in fields:
        kwargs["q"] = _trim(BoardAdminActionLog, "q", (q or "")[:200])

    if "auto_only" in fields:
        kwargs["auto_only"] = bool(auto_only)

    if "report_ids" in fields:
        kwargs["report_ids"] = _trim(BoardAdminActionLog, "report_ids", csv)
    if "report_ids_preview" in fields:
        kwargs["report_ids_preview"] = _trim(BoardAdminActionLog, "report_ids_preview", preview)
    if "report_ids_blob" in fields:
        kwargs["report_ids_blob"] = _trim(BoardAdminActionLog, "report_ids_blob", blob)

    if "filter_qs" in fields:
        kwargs["filter_qs"] = _trim(BoardAdminActionLog, "filter_qs", filter_qs or "")
    if "query_string" in fields:
        kwargs["query_string"] = _trim(
            BoardAdminActionLog,
            "query_string",
            (query_string or filter_qs or ""),
        )

    if "path" in fields:
        kwargs["path"] = _trim(BoardAdminActionLog, "path", path or request.path)

    if "note" in fields:
        kwargs["note"] = note
    elif "message" in fields:
        kwargs["message"] = note

    if "ip" in fields:
        kwargs["ip"] = _trim(BoardAdminActionLog, "ip", _admin_ip(request))
    if "ua" in fields:
        kwargs["ua"] = _trim(BoardAdminActionLog, "ua", _admin_ua(request))

    try:
        BoardAdminActionLog.objects.create(**kwargs)
    except Exception as e:
        try:
            log.exception("[board admin log] create failed")
        except Exception:
            pass
        try:
            print("[board admin log] create failed:", e, "kwargs keys=", list(kwargs.keys()))
        except Exception:
            pass


def _log_action_bulk_resolve() -> str:
    v = getattr(getattr(BoardAdminActionLog, "Action", None), "BULK_RESOLVE", "bulk_resolve")
    return _choice_value(v, "bulk_resolve")


def _log_action_bulk_reject() -> str:
    v = getattr(getattr(BoardAdminActionLog, "Action", None), "BULK_REJECT", "bulk_reject")
    return _choice_value(v, "bulk_reject")


def _log_action_report_action() -> str:
    v = getattr(getattr(BoardAdminActionLog, "Action", None), "REPORT_ACTION", "report_action")
    return _choice_value(v, "report_action")


# ─────────────────────────────────────────────────────────────
# My Tabs  ✅ 통합 + 운영자 전용
# ─────────────────────────────────────────────────────────────

def staff_mine_redirect(request: HttpRequest) -> HttpResponse:
    if not _is_staff(getattr(request, "user", None)):
        raise Http404
    return redirect("board:mine")


def staff_mine(request: HttpRequest) -> HttpResponse:
    if not _is_staff(getattr(request, "user", None)):
        raise Http404

    user = request.user

    posts = (
        BoardPost.objects
        .select_related("category")
        .filter(author=user)
        .order_by("-created_at")[:200]
    )

    comments = (
        BoardComment.objects
        .select_related("post", "post__category")
        .filter(author=user)
        .order_by("-created_at")[:300]
    )

    return render(request, "ragapp/board/mine.html", {
        "tab": "mine",
        "posts": posts,
        "comments": comments,
        "categories": _categories_for_sidebar(request),
        "is_staff": True,
    })


def mine_posts(request: HttpRequest) -> HttpResponse:
    return staff_mine_redirect(request)


def mine_comments(request: HttpRequest) -> HttpResponse:
    return staff_mine_redirect(request)


# ─────────────────────────────────────────────────────────────
# Reports (staff) - list + banner + recent logs
# ─────────────────────────────────────────────────────────────

def staff_reports(request: HttpRequest) -> HttpResponse:
    if not _is_staff(getattr(request, "user", None)):
        raise Http404

    status = (request.GET.get("status") or "open").strip()
    q = (request.GET.get("q") or "").strip()
    auto_only = (request.GET.get("auto") or "").strip() == "1"

    qs = (
        BoardReport.objects
        .select_related("post", "comment", "comment__post", "handled_by", "reporter")
        .all()
    )

    if status in ("open", "resolved", "rejected"):
        qs = qs.filter(status=status)

    if auto_only:
        qs = qs.filter(message__startswith="[AUTO]")

    if q:
        qs = qs.filter(
            Q(message__icontains=q) |
            Q(admin_note__icontains=q) |
            Q(reason__icontains=q) |
            Q(reporter_fp__icontains=q)
        )

    reports = qs.order_by("-created_at")[:300]

    bulk_done: Optional[int]
    try:
        bulk_done = int(request.GET.get("bulk_done") or "")
    except Exception:
        bulk_done = None
    bulk_action = (request.GET.get("bulk_action") or "").strip()

    raw_qs = request.META.get("QUERY_STRING") or ""
    qs_for_bulk = _strip_bulk_params(raw_qs)

    recent_logs = (
        BoardAdminActionLog.objects
        .select_related("actor")
        .filter(action__in=[_log_action_bulk_resolve(), _log_action_bulk_reject(), _log_action_report_action()])
        .order_by("-created_at")[:10]
    )

    return render(request, "ragapp/board/report_list.html", {
        "tab": "reports",
        "reports": reports,
        "status": status,
        "q": q,
        "auto_only": auto_only,
        "categories": _categories_for_sidebar(request),
        "is_staff": True,

        "bulk_done": bulk_done,
        "bulk_action": bulk_action,
        "qs_for_bulk": qs_for_bulk,

        "recent_logs": recent_logs,
    })


# ─────────────────────────────────────────────────────────────
# ✅ BULK: “현재 필터된 목록 그대로” 일괄 처리 (resolve/reject)
# ─────────────────────────────────────────────────────────────

@require_http_methods(["POST"])
def staff_reports_bulk_action(request: HttpRequest) -> HttpResponse:
    if not _is_staff(getattr(request, "user", None)):
        raise Http404

    action = (request.POST.get("action") or "").strip().lower()
    if action not in ("resolve", "reject"):
        # (호환) 과거 스펙: to=resolved|rejected
        to = (request.POST.get("to") or "").strip().lower()
        if to == "resolved":
            action = "resolve"
        elif to == "rejected":
            action = "reject"
        else:
            return redirect("board:reports")

    next_url = (request.POST.get("next") or "").strip()
    qs_text = (request.POST.get("qs") or "").strip()

    filtered_qs = _filtered_reports_from_qs(qs_text)

    limit = _get_int_setting("BOARD_REPORTS_PAGE_LIMIT", 300)
    limit = max(50, min(limit, 2000))

    ids = list(filtered_qs.values_list("id", flat=True)[:limit])
    if not ids:
        back = next_url if next_url.startswith("/") else "/board/mod/reports/"
        return redirect(_append_qs(back, {"bulk_done": "0", "bulk_action": action}))

    now = timezone.now()

    template = (request.POST.get("template") or "").strip()
    if not template:
        ts = timezone.localtime(now).strftime("%Y-%m-%d %H:%M")
        template = f"[일괄종결] 확인 완료 · {ts}" if action == "resolve" else f"[일괄반려] 오탐 정리 · {ts}"

    status_resolved = _status_value("RESOLVED", "resolved")
    status_rejected = _status_value("REJECTED", "rejected")

    with transaction.atomic():
        rows = list(BoardReport.objects.select_for_update().filter(id__in=ids))

        done = 0
        for r in rows:
            r.status = status_resolved if action == "resolve" else status_rejected

            prev = (r.admin_note or "").strip()
            r.admin_note = template if not prev else (prev + "\n" + template)

            r.handled_by = request.user
            r.handled_at = now
            r.save(update_fields=["status", "admin_note", "handled_by", "handled_at"])
            done += 1

        qd = QueryDict(qs_text or "", mutable=False)
        f_status = (qd.get("status") or "open").strip()
        f_q = (qd.get("q") or "").strip()
        f_auto_only = (qd.get("auto") or "").strip() == "1"

        _adminlog_create(
            request,
            action_value=_log_action_bulk_resolve() if action == "resolve" else _log_action_bulk_reject(),
            done=int(done),
            status=f_status,
            q=f_q,
            auto_only=bool(f_auto_only),
            report_ids=ids[:20],
            note=template,
            filter_qs=_strip_bulk_params(qs_text),
            query_string=_strip_bulk_params(qs_text),
            path=request.path,
        )

    back = next_url if next_url.startswith("/") else "/board/mod/reports/"
    return redirect(_append_qs(back, {"bulk_done": str(done), "bulk_action": action}))


# ─────────────────────────────────────────────────────────────
# ✅ 운영 로그 화면 (DB)
# ─────────────────────────────────────────────────────────────

def staff_admin_logs(request: HttpRequest) -> HttpResponse:
    if not _is_staff(getattr(request, "user", None)):
        raise Http404

    def _safe_int(v: str, default: int) -> int:
        try:
            return int(str(v).strip())
        except Exception:
            return default

    def _clamp(n: int, lo: int, hi: int) -> int:
        return max(lo, min(hi, n))

    def _has_field(name: str) -> bool:
        try:
            return any(getattr(f, "name", None) == name for f in BoardAdminActionLog._meta.get_fields())
        except Exception:
            return False

    f_action = (request.GET.get("action") or "").strip()
    f_actor = (request.GET.get("actor") or "").strip()
    f_q = (request.GET.get("q") or "").strip()
    f_report_id = (request.GET.get("report_id") or "").strip()

    since_days = _safe_int(request.GET.get("days") or "", 30)
    since_days = _clamp(since_days, 1, 365)

    page = _safe_int(request.GET.get("page") or "", 1)
    page = _clamp(page, 1, 999999)

    page_size = _safe_int(request.GET.get("ps") or "", 50)
    page_size = _clamp(page_size, 20, 200)

    qs = BoardAdminActionLog.objects.select_related("actor").all()

    # ✅ 여기! timezone.timedelta ❌ -> datetime.timedelta ✅
    try:
        since = timezone.now() - timedelta(days=since_days)
        qs = qs.filter(created_at__gte=since)
    except Exception:
        pass

    if f_action:
        qs = qs.filter(action=f_action)

    if f_actor:
        cond = Q(actor__username__icontains=f_actor)
        if _has_field("actor_name"):
            cond = cond | Q(actor_name__icontains=f_actor)
        qs = qs.filter(cond)

    if f_report_id:
        rid = _safe_int(f_report_id, 0)
        if rid > 0:
            if _has_field("report_ids_blob"):
                qs = qs.filter(report_ids_blob__contains=f",{rid},")
            elif _has_field("report_ids_preview"):
                qs = qs.filter(report_ids_preview__icontains=str(rid))
            elif _has_field("report_ids"):
                s = str(rid)
                qs = qs.filter(
                    Q(report_ids__exact=s) |
                    Q(report_ids__startswith=s + ",") |
                    Q(report_ids__endswith="," + s) |
                    Q(report_ids__contains="," + s + ",")
                )

    if f_q:
        cond = Q()
        if _has_field("note"):
            cond |= Q(note__icontains=f_q)
        if _has_field("filter_qs"):
            cond |= Q(filter_qs__icontains=f_q)
        if _has_field("query_string"):
            cond |= Q(query_string__icontains=f_q)
        if _has_field("path"):
            cond |= Q(path__icontains=f_q)
        if _has_field("status"):
            cond |= Q(status__icontains=f_q)
        if _has_field("q"):
            cond |= Q(q__icontains=f_q)
        if _has_field("report_ids_preview"):
            cond |= Q(report_ids_preview__icontains=f_q)
        if _has_field("report_ids_blob"):
            cond |= Q(report_ids_blob__icontains=f_q)
        if _has_field("report_ids"):
            cond |= Q(report_ids__icontains=f_q)
        if _has_field("ua"):
            cond |= Q(ua__icontains=f_q)
        if _has_field("ip"):
            cond |= Q(ip__icontains=f_q)
        if _has_field("actor_name"):
            cond |= Q(actor_name__icontains=f_q)

        cond |= Q(actor__username__icontains=f_q)
        qs = qs.filter(cond)

    total = qs.count()
    qs = qs.order_by("-created_at")

    start = (page - 1) * page_size
    end = start + page_size
    logs = list(qs[start:end])

    has_prev = page > 1
    has_next = end < total

    try:
        action_choices = list(getattr(BoardAdminActionLog, "Action").choices)  # type: ignore
    except Exception:
        action_choices = []

    return render(request, "ragapp/board/admin_logs.html", {
        "tab": "admin_logs",
        "categories": _categories_for_sidebar(request),
        "is_staff": True,

        "logs": logs,
        "total": total,
        "since_days": since_days,

        "f_action": f_action,
        "f_actor": f_actor,
        "f_q": f_q,
        "f_report_id": f_report_id,

        "page": page,
        "page_size": page_size,
        "has_prev": has_prev,
        "has_next": has_next,

        "action_choices": action_choices,
    })


# ─────────────────────────────────────────────────────────────
# Ban/Link-Restriction helpers (cache 기반)
# ─────────────────────────────────────────────────────────────

def _ban_key(fp: str) -> str:
    return f"board:ban:{fp}"


def _ban_score_key(fp: str) -> str:
    return f"board:ban_score:{fp}"


def _ban_index_key() -> str:
    return "board:ban_index:v1"


def _report_hits_key(fp: str) -> str:
    return f"board:fp_report_hits:{fp}"


def _linkblock_key(fp: str) -> str:
    return f"board:linkblock:{fp}"


def _linkblock_index_key() -> str:
    return "board:linkblock_index:v1"


def _seconds_left(info) -> int | None:
    try:
        if isinstance(info, dict) and "until" in info:
            return max(0, int(info["until"]) - int(time.time()))
    except Exception:
        pass
    return None


def _auto_ban_points(action: str, reason: str) -> int:
    base = 0
    if action == "resolve_hide":
        base = 1
    elif action == "resolve_delete":
        base = 2

    if base:
        rr = (reason or "").strip().lower()
        if rr in ("spam", "ad", "ads"):
            base += 1
    return base


def _ban_add_points(fp: str, points: int, *, meta_reason: str = "auto") -> dict:
    fp = (fp or "").strip().lower()
    if not _FP_RE.match(fp):
        return {
            "ok": False,
            "fp": fp,
            "score": 0,
            "threshold": _get_int_setting("BOARD_BAN_THRESHOLD", 3),
            "banned": False,
            "until": 0,
        }

    if _banlib_add_points is not None:
        try:
            return _banlib_add_points(fp, int(points))  # type: ignore
        except Exception:
            pass

    threshold = _get_int_setting("BOARD_BAN_THRESHOLD", 3)
    ttl = _get_int_setting("BOARD_BAN_TTL_SEC", 86400)

    key = _ban_score_key(fp)
    cur = cache.get(key)

    if cur is None:
        score = int(points)
        cache.set(key, score, ttl)
    else:
        try:
            score = int(cache.incr(key, int(points)))  # type: ignore
        except Exception:
            try:
                score = int(cur) + int(points)
            except Exception:
                score = int(points)
            cache.set(key, score, ttl)

    if score >= threshold:
        ok, until = set_ban(fp, ttl, meta={"reason": meta_reason, "score": score, "ts": int(time.time())})
        try:
            cache.delete(key)
        except Exception:
            pass
        return {
            "ok": bool(ok),
            "fp": fp,
            "score": score,
            "threshold": threshold,
            "banned": bool(ok),
            "until": int(until or 0),
        }

    return {"ok": True, "fp": fp, "score": score, "threshold": threshold, "banned": False, "until": 0}


def _incr_report_hits(fp: str) -> int:
    fp = (fp or "").strip().lower()
    if not _FP_RE.match(fp):
        return 0
    ttl = _get_int_setting("BOARD_REPORT_HITS_TTL_SEC", 30 * 86400)
    key = _report_hits_key(fp)
    cur = cache.get(key)
    if cur is None:
        cache.set(key, 1, ttl)
        return 1
    try:
        return int(cache.incr(key, 1))  # type: ignore
    except Exception:
        try:
            v = int(cur) + 1
        except Exception:
            v = 1
        cache.set(key, v, ttl)
        return v


def _linkblock_index_upsert(fp: str, until: int) -> None:
    fp = (fp or "").strip().lower()
    if not _FP_RE.match(fp):
        return

    key = _linkblock_index_key()
    ttl = _get_int_setting("BOARD_LINKBLOCK_INDEX_TTL_SEC", 7 * 86400)
    now = int(time.time())

    try:
        lst = cache.get(key) or []
        if not isinstance(lst, list):
            lst = []
    except Exception:
        lst = []

    found = False
    for it in lst:
        if isinstance(it, dict) and it.get("fp") == fp:
            it["until"] = int(until)
            it["updated"] = now
            found = True
            break

    if not found:
        lst.insert(0, {"fp": fp, "until": int(until), "updated": now})

    if len(lst) > 500:
        lst = lst[:500]

    try:
        cache.set(key, lst, ttl)
    except Exception:
        pass


def _set_linkblock(fp: str, seconds: int, meta: Dict[str, Any] | None = None) -> Tuple[bool, int]:
    fp = (fp or "").strip().lower()
    if not _FP_RE.match(fp):
        return (False, 0)

    seconds = int(seconds)
    if seconds < 600:
        seconds = 600
    if seconds > 30 * 86400:
        seconds = 30 * 86400

    until = int(time.time()) + seconds
    payload: Dict[str, Any] = {"until": until}
    if meta:
        payload.update(meta)

    try:
        cache.set(_linkblock_key(fp), payload, seconds)
    except Exception:
        return (False, 0)

    _linkblock_index_upsert(fp, until)
    return (True, until)


def _clear_linkblock(fp: str) -> None:
    fp = (fp or "").strip().lower()
    if not _FP_RE.match(fp):
        return
    try:
        cache.delete(_linkblock_key(fp))
    except Exception:
        pass


def _instant_ban_ttl() -> int:
    return _get_int_setting("BOARD_INSTANT_BAN_TTL_SEC", 86400)


def _linkblock_ttl() -> int:
    return _get_int_setting("BOARD_LINKBLOCK_TTL_SEC", 7 * 86400)


# ─────────────────────────────────────────────────────────────
# 신고 단건 처리 + 삭제즉시ban + 신고2회 링크제한 + 3회 승격Ban
# ─────────────────────────────────────────────────────────────

@require_http_methods(["POST"])
def staff_report_action(request: HttpRequest, report_id: int) -> HttpResponse:
    if not _is_staff(getattr(request, "user", None)):
        raise Http404

    r = get_object_or_404(
        BoardReport.objects.select_related("post", "comment", "comment__post", "handled_by"),
        pk=report_id,
    )

    action = (request.POST.get("action") or "").strip().lower()
    to = (request.POST.get("to") or "").strip().lower()

    if action in ("resolve", "resolved"):
        action = "resolve"
        final_status = _status_value("RESOLVED", "resolved")
    elif action in ("reject", "rejected"):
        action = "reject"
        final_status = _status_value("REJECTED", "rejected")
    elif action in ("resolve_hide", "resolve_delete", "resolve_delete_ban"):
        final_status = _status_value("RESOLVED", "resolved")
    elif to in ("resolved", "rejected"):
        final_status = _status_value("RESOLVED", "resolved") if to == "resolved" else _status_value("REJECTED", "rejected")
        action = "resolve" if to == "resolved" else "reject"
    else:
        return redirect("board:reports")

    note = (request.POST.get("admin_note") or "").strip()
    use_template = (request.POST.get("use_template") or "1").strip() != "0"
    if use_template and not note:
        note = _build_default_note(r, action)

    now = timezone.now()
    next_url = (request.POST.get("next") or "").strip()

    next_qs = ""
    f_status = "open"
    f_q = ""
    f_auto_only = False
    if next_url.startswith("/"):
        try:
            u = urlsplit(next_url)
            next_qs = u.query or ""
            qd = QueryDict(next_qs, mutable=False)
            f_status = (qd.get("status") or "open").strip()
            f_q = (qd.get("q") or "").strip()
            f_auto_only = (qd.get("auto") or "").strip() == "1"
        except Exception:
            pass

    target_fp = ""
    target_is_staff_author = False

    if action in ("resolve_hide", "resolve_delete", "resolve_delete_ban"):
        if r.target_type == BoardReport.TargetType.POST and r.post_id and isinstance(r.post, BoardPost):
            p = r.post
            target_fp = (getattr(p, "creator_fp", "") or "").strip().lower()
            a = getattr(p, "author", None)
            try:
                if a and (getattr(a, "is_staff", False) or getattr(a, "is_superuser", False)):
                    target_is_staff_author = True
            except Exception:
                pass

        elif r.target_type == BoardReport.TargetType.COMMENT and r.comment_id and isinstance(r.comment, BoardComment):
            c = r.comment
            target_fp = (getattr(c, "creator_fp", "") or "").strip().lower()
            a = getattr(c, "author", None)
            try:
                if a and (getattr(a, "is_staff", False) or getattr(a, "is_superuser", False)):
                    target_is_staff_author = True
            except Exception:
                pass

    with transaction.atomic():
        r.status = final_status
        r.admin_note = note
        r.handled_by = request.user
        r.handled_at = now
        r.save(update_fields=["status", "admin_note", "handled_by", "handled_at"])

        if action in ("resolve_hide", "resolve_delete", "resolve_delete_ban"):
            if r.target_type == BoardReport.TargetType.POST and r.post_id:
                p = r.post  # type: ignore
                if isinstance(p, BoardPost):
                    if action == "resolve_hide":
                        if p.is_published:
                            p.is_published = False
                        if p.pinned:
                            p.pinned = False
                        p.save(update_fields=["is_published", "pinned", "updated_at"])
                    else:
                        if not p.is_deleted:
                            p.is_deleted = True
                            p.deleted_at = now
                        p.is_published = False
                        p.pinned = False
                        p.save(update_fields=["is_deleted", "deleted_at", "is_published", "pinned", "updated_at"])

            elif r.target_type == BoardReport.TargetType.COMMENT and r.comment_id:
                c = r.comment  # type: ignore
                if isinstance(c, BoardComment):
                    if action == "resolve_hide":
                        if not c.is_hidden:
                            c.is_hidden = True
                            c.hidden_at = now
                        c.save(update_fields=["is_hidden", "hidden_at", "updated_at"])
                    else:
                        if not c.is_deleted:
                            c.is_deleted = True
                            c.deleted_at = now
                        c.save(update_fields=["is_deleted", "deleted_at", "updated_at"])

        if action in ("resolve_hide", "resolve_delete", "resolve_delete_ban"):
            if target_fp and _FP_RE.match(target_fp) and not target_is_staff_author:
                hits = _incr_report_hits(target_fp)

                if hits >= 2:
                    lb_info = cache.get(_linkblock_key(target_fp))
                    if not lb_info or (_seconds_left(lb_info) == 0):
                        ok_lb, _until_lb = _set_linkblock(
                            target_fp,
                            _linkblock_ttl(),
                            meta={
                                "reason": "report_hits",
                                "hits": hits,
                                "by": getattr(request.user, "username", "staff"),
                                "ts": int(time.time()),
                            },
                        )
                        if ok_lb:
                            r.admin_note = (r.admin_note or "") + f"\n[레벨링] fp={target_fp} 신고 {hits}회 → 외부URL 금지"
                            r.save(update_fields=["admin_note"])

                instant_hit = _get_int_setting("BOARD_REPORT_HITS_INSTANT_BAN", 3)
                instant_ttl = _get_int_setting("BOARD_REPORT_HITS_INSTANT_BAN_TTL_SEC", _instant_ban_ttl())

                if hits >= instant_hit and action != "resolve_delete_ban":
                    ok, _until = set_ban(
                        target_fp,
                        instant_ttl,
                        meta={
                            "reason": "report_hits_escalation",
                            "hits": hits,
                            "by": getattr(request.user, "username", "staff"),
                            "report_id": r.id,
                            "ts": int(time.time()),
                        },
                    )
                    if ok:
                        try:
                            cache.delete(_ban_score_key(target_fp))
                        except Exception:
                            pass
                        r.admin_note = (r.admin_note or "") + f"\n[승격Ban] fp={target_fp} 신고 {hits}회 → 즉시Ban({instant_ttl}s)"
                        r.save(update_fields=["admin_note"])

                if action == "resolve_delete_ban":
                    ok, _until = set_ban(
                        target_fp,
                        _instant_ban_ttl(),
                        meta={
                            "reason": "report_delete_instant",
                            "by": getattr(request.user, "username", "staff"),
                            "report_id": r.id,
                            "ts": int(time.time()),
                        },
                    )
                    if ok:
                        try:
                            cache.delete(_ban_score_key(target_fp))
                        except Exception:
                            pass
                        r.admin_note = (r.admin_note or "") + f"\n[즉시Ban] fp={target_fp} → {_instant_ban_ttl()}s"
                        r.save(update_fields=["admin_note"])

                elif action in ("resolve_hide", "resolve_delete"):
                    pts = _auto_ban_points(action, r.reason)
                    if pts > 0:
                        res = _ban_add_points(target_fp, pts, meta_reason="report_action")
                        if res.get("ok"):
                            if res.get("banned"):
                                r.admin_note = (r.admin_note or "") + f"\n[자동Ban] fp={target_fp} → 24h ban"
                            else:
                                r.admin_note = (r.admin_note or "") + f"\n[자동Ban] fp={target_fp} 점수 {res.get('score')}/{res.get('threshold')}"
                            r.save(update_fields=["admin_note"])

        _adminlog_create(
            request,
            action_value=_log_action_report_action(),
            done=1,
            status=(f_status or ""),
            q=(f_q or ""),
            auto_only=bool(f_auto_only),
            report_ids=[int(r.id)],
            note=(f"[{action}] " + (r.admin_note or "")).strip(),
            filter_qs=_strip_bulk_params(next_qs),
            query_string=_strip_bulk_params(next_qs),
            path=request.path,
        )

    if next_url.startswith("/"):
        return redirect(next_url)
    return redirect("board:reports")


# ─────────────────────────────────────────────────────────────
# Ban console (staff)
# ─────────────────────────────────────────────────────────────

def staff_bans(request: HttpRequest) -> HttpResponse:
    if not _is_staff(getattr(request, "user", None)):
        raise Http404

    q = (request.GET.get("fp") or "").strip().lower()

    raw = cache.get(_ban_index_key()) or []
    items = []
    now = int(time.time())

    for it in raw:
        if not isinstance(it, dict):
            continue
        fp = str(it.get("fp") or "").lower()
        until = int(it.get("until") or 0)
        if not fp or until <= 0:
            continue

        if q and q not in fp:
            continue

        ban_info = cache.get(_ban_key(fp))
        if not ban_info:
            continue

        left = _seconds_left(ban_info)
        items.append({
            "fp": fp,
            "until": until,
            "left": left,
            "mins": (left // 60) if isinstance(left, int) else None,
        })

    items.sort(key=lambda x: x["until"])

    raw_lb = cache.get(_linkblock_index_key()) or []
    linkblocks = []
    for it in raw_lb:
        if not isinstance(it, dict):
            continue
        fp = str(it.get("fp") or "").lower()
        until = int(it.get("until") or 0)
        if not fp or until <= 0:
            continue

        if q and q not in fp:
            continue

        info = cache.get(_linkblock_key(fp))
        if not info:
            continue

        left = _seconds_left(info)
        linkblocks.append({
            "fp": fp,
            "until": until,
            "left": left,
            "mins": (left // 60) if isinstance(left, int) else None,
        })

    linkblocks.sort(key=lambda x: x["until"])

    return render(request, "ragapp/board/ban_list.html", {
        "tab": "bans",
        "items": items,
        "linkblocks": linkblocks,
        "q": q,
        "now": now,
        "is_staff": True,
        "categories": _categories_for_sidebar(request),
    })


@require_http_methods(["POST"])
def staff_unban(request: HttpRequest, fp: str) -> HttpResponse:
    if not _is_staff(getattr(request, "user", None)):
        raise Http404

    fp = (fp or "").strip().lower()
    if not _FP_RE.match(fp):
        raise Http404

    cache.delete(_ban_key(fp))
    cache.delete(_ban_score_key(fp))

    next_url = (request.POST.get("next") or "").strip()
    if next_url.startswith("/"):
        return redirect(next_url)
    return redirect("board:bans")


@require_http_methods(["POST"])
def staff_manual_ban(request: HttpRequest) -> HttpResponse:
    if not _is_staff(getattr(request, "user", None)):
        raise Http404

    fp = (request.POST.get("fp") or "").strip().lower()
    if not _FP_RE.match(fp):
        return redirect("board:bans")

    preset = (request.POST.get("duration") or "86400").strip()
    custom_min = (request.POST.get("custom_minutes") or "").strip()

    seconds = 86400
    try:
        seconds = int(preset)
    except Exception:
        seconds = 86400

    if custom_min:
        try:
            seconds = int(custom_min) * 60
        except Exception:
            pass

    set_ban(fp, seconds, meta={
        "reason": "manual",
        "by": getattr(request.user, "username", "staff"),
        "note": (request.POST.get("note") or "").strip(),
        "ts": int(time.time()),
    })

    next_url = (request.POST.get("next") or "").strip()
    if next_url.startswith("/"):
        return redirect(next_url)
    return redirect("board:bans")


@require_http_methods(["POST"])
def staff_clear_linkblock(request: HttpRequest, fp: str) -> HttpResponse:
    if not _is_staff(getattr(request, "user", None)):
        raise Http404

    fp = (fp or "").strip().lower()
    if not _FP_RE.match(fp):
        raise Http404

    _clear_linkblock(fp)

    next_url = (request.POST.get("next") or "").strip()
    if next_url.startswith("/"):
        return redirect(next_url)
    return redirect("board:bans")


@require_http_methods(["POST"])
def staff_manual_linkblock(request: HttpRequest) -> HttpResponse:
    if not _is_staff(getattr(request, "user", None)):
        raise Http404

    fp = (request.POST.get("fp") or "").strip().lower()
    if not _FP_RE.match(fp):
        return redirect("board:bans")

    preset = (request.POST.get("duration") or str(_linkblock_ttl())).strip()
    custom_min = (request.POST.get("custom_minutes") or "").strip()

    seconds = _linkblock_ttl()
    try:
        seconds = int(preset)
    except Exception:
        seconds = _linkblock_ttl()

    if custom_min:
        try:
            seconds = int(custom_min) * 60
        except Exception:
            pass

    _set_linkblock(fp, seconds, meta={
        "reason": "manual_linkblock",
        "by": getattr(request.user, "username", "staff"),
        "note": (request.POST.get("note") or "").strip(),
        "ts": int(time.time()),
    })

    next_url = (request.POST.get("next") or "").strip()
    if next_url.startswith("/"):
        return redirect(next_url)
    return redirect("board:bans")


@require_http_methods(["POST"])
def staff_reporter_ban_boost(request: HttpRequest) -> HttpResponse:
    if not _is_staff(getattr(request, "user", None)):
        raise Http404
    fp = (request.POST.get("fp") or "").strip().lower()
    if not _FP_RE.match(fp):
        return redirect("board:bans")
    set_ban(fp, 86400, meta={"reason": "manual"})
    return redirect("board:bans")


# ─────────────────────────────────────────────────────────────
# 운영자 mine
# ─────────────────────────────────────────────────────────────

@require_GET
@login_required
def staff_mine_summary_api(request: HttpRequest) -> JsonResponse:
    if not _is_staff(getattr(request, "user", None)):
        raise Http404

    u = request.user
    posts_cnt = BoardPost.objects.filter(author=u).count()
    comments_cnt = BoardComment.objects.filter(author=u).count()

    role = "Staff" if (getattr(u, "is_staff", False) or getattr(u, "is_superuser", False)) else "User"

    return JsonResponse({
        "ok": True,
        "posts": int(posts_cnt),
        "comments": int(comments_cnt),
        "role": role,
    })
