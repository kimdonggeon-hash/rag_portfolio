# ragapp/news_views/usage_views.py
from __future__ import annotations

import logging
from datetime import date as date_cls
from typing import Any, Dict, List

from django.conf import settings
from django.http import HttpRequest, JsonResponse, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Sum

from ragapp.services.usage_limiter import build_client_keys, get_daily_limit

log = logging.getLogger(__name__)

try:
    from ragapp.models import DailyUsage  # type: ignore
except Exception:  # pragma: no cover
    DailyUsage = None  # type: ignore

_KIND_TO_FIELD = {
    "web": "web_count",
    "rag": "rag_count",
    "pdf": "pdf_count",
    "image": "image_count",
    "table": "table_count",   # (모델에 남아있어도 OK / 프론트에서 안 쓰면 무시됨)
}

_ADMIN_FIELD = "admin_count"


def _is_admin_unlimited(request: HttpRequest) -> bool:
    user = getattr(request, "user", None)
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))
    )


def _read_today_usage_for_request(request: HttpRequest) -> Dict[str, int]:
    if _is_admin_unlimited(request):
        return {k: 0 for k in _KIND_TO_FIELD.keys()}

    if DailyUsage is None:
        return {k: 0 for k in _KIND_TO_FIELD.keys()}

    try:
        today = timezone.localdate()
        keys = build_client_keys(request)
        rows = list(DailyUsage.objects.filter(date=today, client_key__in=keys))

        out: Dict[str, int] = {k: 0 for k in _KIND_TO_FIELD.keys()}
        for row in rows:
            for kind, field in _KIND_TO_FIELD.items():
                out[kind] = max(out[kind], int(getattr(row, field, 0) or 0))
        return out
    except Exception as e:
        log.exception("usage read failed: %s", e)
        return {k: 0 for k in _KIND_TO_FIELD.keys()}


def _limits_dict() -> Dict[str, int]:
    return {k: int(get_daily_limit(k) or 0) for k in _KIND_TO_FIELD.keys()}


def _remaining(limit: int, used: int) -> int | None:
    if limit <= 0:
        return None
    return max(0, limit - used)


@require_GET
@never_cache
def api_usage_status(request: HttpRequest) -> JsonResponse:
    unlimited = _is_admin_unlimited(request)

    used = _read_today_usage_for_request(request)
    limits = _limits_dict()

    if unlimited:
        remain = {k: None for k in _KIND_TO_FIELD.keys()}
    else:
        remain = {k: _remaining(limits[k], used[k]) for k in _KIND_TO_FIELD.keys()}

    return JsonResponse(
        {
            "ok": True,
            "date": str(timezone.localdate()),
            "limits": limits,
            "used": used,
            "remaining": remain,
            "unlimited": unlimited,
        },
        status=200,
        json_dumps_params={"ensure_ascii": False},
    )


@staff_member_required
@never_cache
def daily_usage_dashboard(request: HttpRequest) -> HttpResponse:
    """
    /ragadmin/usage/  -> templates/ragadmin/daily_usage.html
    ✅ TABLE은 화면에서 제거
    ✅ ADMIN(admin_count) 추가
    """
    selected = timezone.localdate()
    q = (request.GET.get("q") or "").strip()

    ds = (request.GET.get("date") or "").strip()
    if ds:
        try:
            selected = date_cls.fromisoformat(ds)
        except Exception:
            pass

    # limits
    limit_web = get_daily_limit("web")
    limit_rag = get_daily_limit("rag")
    limit_pdf = get_daily_limit("pdf")
    limit_image = get_daily_limit("image")
    limit_admin = "∞"

    rows: List[Dict[str, Any]] = []
    top_rows: List[Dict[str, Any]] = []

    total_web = total_rag = total_pdf = total_image = total_admin = 0
    row_count = 0
    totals_approx = False

    if DailyUsage is not None:
        try:
            qs = DailyUsage.objects.filter(date=selected)
            if q:
                qs = qs.filter(client_key__contains=q)

            # ⚠️ 한 사람당 행이 여러 개다(브라우저별 쿠키 행 + 그 사람들이 공유하는 IP 행).
            # 사용량은 후보 키들 중 max로 판단하고 전부 같은 값으로 동기화하므로,
            # 모든 행을 그냥 Sum하면 같은 사용량을 2~3번 세어 합계가 부풀려진다.
            # IP 행 하나가 그 그룹의 실제 사용량(max)을 들고 있으므로 IP 행만 집계한다.
            ip_qs = qs.filter(key_kind=DailyUsage.KEY_KIND_IP)

            # key_kind가 붙기 전에 쌓인 과거 데이터는 구분할 수 없어서 예전 방식으로 폴백
            totals_approx = not ip_qs.exists()
            agg_qs = qs if totals_approx else ip_qs

            agg = agg_qs.aggregate(
                sw=Sum("web_count"),
                sr=Sum("rag_count"),
                sp=Sum("pdf_count"),
                si=Sum("image_count"),
                sa=Sum(_ADMIN_FIELD),
            )
            total_web = int(agg["sw"] or 0)
            total_rag = int(agg["sr"] or 0)
            total_pdf = int(agg["sp"] or 0)
            total_image = int(agg["si"] or 0)
            total_admin = int(agg["sa"] or 0)

            raw = list(
                qs.order_by("-id").values(
                    "client_key",
                    "key_kind",
                    "web_count",
                    "rag_count",
                    "pdf_count",
                    "image_count",
                    _ADMIN_FIELD,
                )[:2000]
            )
            row_count = len(raw)

            _KIND_LABEL = {
                DailyUsage.KEY_KIND_CID: "쿠키(브라우저)",
                DailyUsage.KEY_KIND_IP: "IP(기기/네트워크)",
            }

            def pack(r: Dict[str, Any]) -> Dict[str, Any]:
                web = int(r.get("web_count") or 0)
                rag = int(r.get("rag_count") or 0)
                pdf = int(r.get("pdf_count") or 0)
                img = int(r.get("image_count") or 0)
                adm = int(r.get(_ADMIN_FIELD) or 0)
                tot = web + rag + pdf + img + adm  # ✅ total에 admin 포함
                kind = r.get("key_kind") or ""
                return {
                    "client_key": r.get("client_key") or "",
                    "key_kind": kind,
                    "key_kind_label": _KIND_LABEL.get(kind, "미분류(과거 데이터)"),
                    "total": tot,
                    "web": web,
                    "rag": rag,
                    "pdf": pdf,
                    "image": img,
                    "admin": adm,
                }

            rows = [pack(r) for r in raw]
            # 상위 목록은 사람 단위로 보이도록 IP 행 기준(과거 데이터면 전체)
            _top_src = [x for x in rows if x["key_kind"] == DailyUsage.KEY_KIND_IP] or rows
            top_rows = sorted(_top_src, key=lambda x: x["total"], reverse=True)[:20]

        except Exception as e:
            log.exception("daily_usage_dashboard failed: %s", e)

    ctx = {
        "selected_date": selected,
        "server_localdate": timezone.localdate(),
        "django_time_zone": getattr(settings, "TIME_ZONE", ""),
        "current_tz_name": timezone.get_current_timezone_name(),
        "q": q,
        # KPI
        "limit_web": limit_web,
        "limit_rag": limit_rag,
        "limit_pdf": limit_pdf,
        "limit_image": limit_image,
        "limit_admin": limit_admin,
        "total_web": total_web,
        "total_rag": total_rag,
        "total_pdf": total_pdf,
        "total_image": total_image,
        "total_admin": total_admin,
        "totals_approx": totals_approx,
        # tables
        "top_rows": top_rows,
        "rows": rows,
        "row_count": row_count,
    }
    return render(request, "ragadmin/daily_usage.html", ctx)


# ✅ 호환용 별칭: admin_site.py가 ragadmin_usage_view를 찾는 경우 대비
ragadmin_usage_view = daily_usage_dashboard
