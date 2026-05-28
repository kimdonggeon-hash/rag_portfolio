from __future__ import annotations

from typing import Optional, Set

from django.conf import settings
from django.http import HttpRequest, JsonResponse, HttpResponse
from django.urls import resolve

from ragapp.services.usage_limiter import check_and_increment_usage


class UsageQuotaMiddleware:
    """
    ✅ 업로드(POST)만 제한. (이미지/표 업로드 화면에서 1회 제출 = 1회 카운트)
    - 매칭 우선순위:
      1) settings.USAGE_QUOTA_UPLOAD_MAP (path prefix)
      2) url_name ("media_index", "table_index")

    ✅ 같은 요청에서 중복 호출 방지(아이템포턴트):
      - request 객체에 kind별 처리 여부를 기록해서 1번만 차감되게 함.

    ✅ 운영 정책:
      - 스태프/운영콘솔(ragadmin/admin)은 무제한 통과 (막히는 상황 방지)
    """

    REQ_ATTR = "_usage_quota_mw_done_kinds"

    def __init__(self, get_response):
        self.get_response = get_response

    def _match_kind(self, request: HttpRequest) -> Optional[str]:
        if request.method != "POST":
            return None

        path = request.path_info or ""

        # 1) prefix map (가장 안정적)
        mp = getattr(settings, "USAGE_QUOTA_UPLOAD_MAP", None) or {}
        if isinstance(mp, dict):
            # 긴 prefix가 먼저 매칭되도록 정렬
            for prefix in sorted(mp.keys(), key=lambda x: len(str(x)), reverse=True):
                pfx = str(prefix)
                if pfx and path.startswith(pfx):
                    kind = mp.get(prefix)
                    if kind:
                        return str(kind)

        # 2) url_name fallback
        try:
            url_name = resolve(path).url_name or ""
        except Exception:
            url_name = ""

        if url_name == "media_index":
            return "image"
        if url_name == "table_index":
            return "table"

        return None

    def _wants_json(self, request: HttpRequest) -> bool:
        accept = (request.headers.get("Accept") or "").lower()
        xrw = (request.headers.get("X-Requested-With") or "").lower()
        if request.path_info.startswith("/api/"):
            return True
        if "application/json" in accept:
            return True
        if xrw == "xmlhttprequest":
            return True
        return False

    def __call__(self, request: HttpRequest):
        # ✅ 스태프/운영콘솔은 무제한 통과 (막히는 상황 방지)
        u = getattr(request, "user", None)
        if u and getattr(u, "is_staff", False):
            return self.get_response(request)

        path = request.path_info or ""
        if path.startswith("/ragadmin/") or path.startswith("/admin/"):
            return self.get_response(request)

        kind = self._match_kind(request)
        if not kind:
            return self.get_response(request)

        # ✅ 같은 요청에서 동일 kind 중복 차감 방지
        done: Set[str] = getattr(request, self.REQ_ATTR, None)
        if done is None:
            done = set()
            setattr(request, self.REQ_ATTR, done)

        if kind in done:
            return self.get_response(request)
        done.add(kind)

        allowed, limit, used_after = check_and_increment_usage(request, kind)

        if allowed:
            return self.get_response(request)

        # ❌ 제한 초과
        status = 429
        if self._wants_json(request):
            return JsonResponse(
                {
                    "ok": False,
                    "error": "DAILY_LIMIT_REACHED",
                    "kind": kind,
                    "limit": limit,
                    "used": used_after,
                    "message": f"오늘 {kind} 업로드는 {limit}회까지 가능합니다.",
                },
                status=status,
                json_dumps_params={"ensure_ascii": False},
            )

        return HttpResponse(
            f"""
<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>오늘 사용량 초과</title>
<body style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Noto Sans KR,sans-serif;padding:24px">
<h2 style="margin:0 0 10px">오늘 업로드 횟수를 다 썼어요</h2>
<p style="margin:0 0 14px;line-height:1.6">
오늘 <b>{kind}</b> 업로드는 <b>{limit}회</b>까지 가능합니다.<br>
내일 다시 시도해 주세요.
</p>
<p style="margin:0"><a href="javascript:history.back()">← 돌아가기</a></p>
</body></html>
            """.strip(),
            status=status,
            content_type="text/html; charset=utf-8",
        )
