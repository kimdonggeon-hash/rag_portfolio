# ragapp/middleware/pii_block.py

from __future__ import annotations

import json
from typing import Any

from django.conf import settings
from django.http import HttpResponse, JsonResponse

from ragapp.utils.pii_guard import detect_pii_any


def _get_setting(name: str, default: Any) -> Any:
    return getattr(settings, name, default)


def _is_html_request(request) -> bool:
    accept = (request.headers.get("Accept") or "").lower()
    # 브라우저는 보통 text/html을 선호
    return "text/html" in accept and "application/json" not in accept


def _qdict_to_plain(qd) -> dict[str, Any]:
    """
    QueryDict / MultiValueDict를 detect_pii_any가 탐지 가능한
    일반 dict 구조로 변환:
      - key: str
      - value: list[str] (여러 값도 안전하게 전부 검사)
    """
    try:
        return {k: vlist for k, vlist in qd.lists()}
    except Exception:
        # 최후의 폴백
        try:
            return dict(qd)
        except Exception:
            return {}


class PiiBlockMiddleware:
    """
    유틸(detect_pii_any)을 이용해 전역 PII 입력을 차단하는 미들웨어.
    - GET QueryString / JSON Body / Form & Multipart 텍스트 필드 검사
    - 파일 "내용"은 기본 검사하지 않음(성능/안정성). 파일명은 검사.
    - 친절한 안내 문구로 응답
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not _get_setting("PII_BLOCK_ENABLED", True):
            return self.get_response(request)

        path = request.path or "/"

        # ✅ 제외 경로 (헬스체크/웰노운/정적 등)
        if path in ("/healthz", "/healthz/"):
            return self.get_response(request)
        if path.startswith("/.well-known/"):
            return self.get_response(request)

        excluded_prefixes = _get_setting("PII_BLOCK_EXCLUDED_PATH_PREFIXES", ())
        for pfx in excluded_prefixes:
            if path.startswith(pfx):
                return self.get_response(request)

        max_scan_bytes = int(_get_setting("PII_BLOCK_MAX_SCAN_BYTES", 200_000))

        # ✅ 상태코드(200/400 등) settings에서 제어
        block_status = int(_get_setting("PII_BLOCK_STATUS", 400))

        # 안내 문구(원하는대로 커스텀 가능)
        msg_title = _get_setting("PII_BLOCK_TITLE", "민감정보 입력이 감지되었습니다")
        msg_body = _get_setting(
            "PII_BLOCK_MESSAGE",
            "카드번호/계좌번호/주민번호/전화번호/이메일/주소 등 개인정보는 입력할 수 없습니다.\n"
            "해당 내용을 삭제(또는 마스킹)한 뒤 다시 시도해주세요.\n"
            "예) 1234-****-****-5678 / ****1234",
        )

        include_kind = bool(_get_setting("PII_BLOCK_INCLUDE_KIND", True))
        stealth_404 = bool(_get_setting("PII_BLOCK_STEALTH_404", False))

        def blocked(kind: str | None):
            # 스텔스 모드(원하면 404로)
            if stealth_404:
                return HttpResponse(status=404)

            # 브라우저/페이지 접근이면 HTML로 친절 안내
            if _is_html_request(request):
                html = f"""
                <html>
                  <head><meta charset="utf-8"><title>{msg_title}</title></head>
                  <body style="font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; line-height:1.6; padding: 24px;">
                    <h2>{msg_title}</h2>
                    <p style="white-space: pre-wrap;">{msg_body}</p>
                    {"<p><b>감지 항목:</b> " + (kind or "PII") + "</p>" if include_kind else ""}
                  </body>
                </html>
                """
                return HttpResponse(html, status=block_status, content_type="text/html; charset=utf-8")

            # API/앱 접근이면 JSON
            payload = {"detail": msg_body, "code": "PII_BLOCKED"}
            if include_kind:
                payload["kind"] = kind or "PII"
            return JsonResponse(payload, status=block_status)

        # ------------------------------------------------------------
        # 1) QueryString 검사 (QueryDict → plain dict로 변환 후 검사)
        # ------------------------------------------------------------
        r = detect_pii_any(_qdict_to_plain(request.GET))
        if r.hit:
            return blocked(r.kind)

        # ------------------------------------------------------------
        # 2) Body 검사 (Content-Type 별)
        # ------------------------------------------------------------
        ctype = (request.content_type or "").lower()

        # 큰 요청은 바디 전체 스캔하지 않음(기본 200KB)
        clen = request.META.get("CONTENT_LENGTH")
        try:
            if clen and int(clen) > max_scan_bytes:
                # 큰 multipart/form은 텍스트 필드만 검사
                if "multipart/form-data" in ctype or "application/x-www-form-urlencoded" in ctype:
                    r2 = detect_pii_any(_qdict_to_plain(request.POST))
                    if r2.hit:
                        return blocked(r2.kind)
                return self.get_response(request)
        except Exception:
            pass

        # JSON
        if "application/json" in ctype:
            raw = (request.body or b"").decode("utf-8", errors="ignore")

            # raw 자체도 검사(깨진 JSON이어도 잡힘)
            r0 = detect_pii_any(raw)
            if r0.hit:
                return blocked(r0.kind)

            try:
                data = json.loads(raw) if raw.strip() else {}
            except Exception:
                data = None

            if data is not None:
                r1 = detect_pii_any(data)
                if r1.hit:
                    return blocked(r1.kind)

            return self.get_response(request)

        # Form / Multipart 텍스트 필드
        if ("application/x-www-form-urlencoded" in ctype) or ("multipart/form-data" in ctype):
            r3 = detect_pii_any(_qdict_to_plain(request.POST))
            if r3.hit:
                return blocked(r3.kind)

            # 파일명 검사(파일 내용은 기본 제외)
            for f in request.FILES.values():
                name = getattr(f, "name", "") or ""
                if name:
                    r4 = detect_pii_any(name)
                    if r4.hit:
                        return blocked(r4.kind)

            return self.get_response(request)

        # 기타: 디코드 가능한 바디만 가볍게 검사
        raw2 = (request.body or b"").decode("utf-8", errors="ignore")
        if raw2:
            r5 = detect_pii_any(raw2)
            if r5.hit:
                return blocked(r5.kind)

        return self.get_response(request)