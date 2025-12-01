# ragapp/legal_views.py
from __future__ import annotations

import os
import hashlib
import json
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.http import require_http_methods

# 메인 법무 뷰는 news_views/legal_views.py 에서 가져와 재노출한다.
try:
    from ragapp.news_views.legal_views import (  # type: ignore
        legal_privacy,
        legal_tos,
        legal_overseas,
        legal_tester,
        legal_guide,
        legal_bundle,
    )
except Exception:
    # 만약 import 실패 시, 500 방지용 더미 핸들러 (개발 중 임시)
    def _err(_req, msg="legal views not ready"):
        return HttpResponse(msg, status=500, content_type="text/plain; charset=utf-8")

    legal_privacy = lambda req: _err(req, "legal_privacy missing")  # type: ignore
    legal_tos = lambda req: _err(req, "legal_tos missing")  # type: ignore
    legal_overseas = lambda req: _err(req, "legal_overseas missing")  # type: ignore
    legal_tester = lambda req: _err(req, "legal_tester missing")  # type: ignore
    legal_guide = lambda req: _err(req, "legal_guide missing")  # type: ignore
    def legal_bundle(_req: HttpRequest) -> JsonResponse:  # type: ignore
        return JsonResponse({"ok": False, "error": "legal_bundle missing"}, status=500)


# ─────────────────────────────────────────────────────────────
# 화면에 노출할 모델명은 .env만 신뢰 (settings 무시)
# ─────────────────────────────────────────────────────────────
def _env_model_display() -> str:
    for k in (
        "GEMINI_MODEL_DIRECT",
        "GEMINI_TEXT_MODEL",
        "VERTEX_TEXT_MODEL",
        "GEMINI_MODEL",
        "GEMINI_MODEL_DEFAULT",
    ):
        v = os.environ.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "[.env에 모델 미설정]"


# ─────────────────────────────────────────────────────────────
# utils
# ─────────────────────────────────────────────────────────────
def _client_ip(req: HttpRequest) -> str:
    xf = req.META.get("HTTP_X_FORWARDED_FOR")
    if xf:
        return xf.split(",")[0].strip()
    return req.META.get("REMOTE_ADDR", "") or ""

def _hash_ip(ip: str) -> str:
    secret = getattr(settings, "LOG_IP_HASH_SECRET", "") or ""
    h = hashlib.sha256()
    h.update((ip + "|" + secret).encode("utf-8", errors="ignore"))
    return h.hexdigest()

def _json_ok(payload: Dict[str, Any], status: int = 200) -> JsonResponse:
    payload.setdefault("ok", True)
    resp = JsonResponse(payload, status=status, json_dumps_params={"ensure_ascii": False})
    resp["Cache-Control"] = "no-store"
    return resp

def _json_fail(msg: str, *, status: int = 400, extra: Optional[Dict[str, Any]] = None) -> JsonResponse:
    body = {"ok": False, "error": msg}
    if extra:
        body.update(extra)
    resp = JsonResponse(body, status=status, json_dumps_params={"ensure_ascii": False})
    resp["Cache-Control"] = "no-store"
    return resp


# ─────────────────────────────────────────────────────────────
# robots.txt 동적 제공
# ─────────────────────────────────────────────────────────────
def robots_txt(request: HttpRequest):
    """
    - settings.NOINDEX_ENABLED=True 이면 전체 차단
    - 기본은 Allow / admin, ragadmin 차단
    - settings.SITEMAP_URL 있으면 Sitemap 추가
    """
    noindex = getattr(settings, "NOINDEX_ENABLED", False)
    if noindex:
        lines = ["User-agent: *", "Disallow: /"]
    else:
        lines = ["User-agent: *", "Allow: /", "Disallow: /admin/", "Disallow: /ragadmin/"]

    sitemap = getattr(settings, "SITEMAP_URL", None)
    if sitemap:
        lines.append(f"Sitemap: {sitemap}")

    resp = HttpResponse("\n".join(lines) + "\n", content_type="text/plain; charset=utf-8")
    resp["Cache-Control"] = "public, max-age=3600"
    return resp


# ─────────────────────────────────────────────────────────────
# 최소 버전 개인정보 페이지(폴백용)
# ─────────────────────────────────────────────────────────────
def privacy_page(request: HttpRequest):
    """
    정식 페이지는 legal_privacy(템플릿 기반)를 쓰고,
    이 페이지는 링크가 없을 때 보여줄 '단일 파일 폴백' 용도.
    """
    def yn(b): return "켜짐" if b else "꺼짐"

    SAFE_MODE_ENABLED = getattr(settings, "SAFE_MODE_ENABLED", True)
    SAFE_SUMMARY_ONLY = getattr(settings, "SAFE_SUMMARY_ONLY", True)
    RESPECT_ROBOTS = getattr(settings, "RESPECT_ROBOTS", True)
    STORE_FULLTEXT = getattr(settings, "STORE_FULLTEXT", False)
    LOG_IP_HASHED = getattr(settings, "LOG_IP_HASHED", False)
    RETENTION_DAYS = int(getattr(settings, "RETENTION_DAYS", 0) or 0)
    PRIVACY_PAGE_URL = getattr(settings, "PRIVACY_PAGE_URL", "") or ""
    MODEL_NAME = _env_model_display()

    html = f"""<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>개인정보 처리 안내</title>
<style>
 body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; line-height:1.6; margin: 24px; color:#0f172a; }}
 h1 {{ font-size: 1.5rem; margin-bottom: .5rem; }}
 section {{ margin: 1.25rem 0; padding: 1rem; border: 1px solid #e2e8f0; border-radius: .75rem; background:#f8fafc; }}
 code {{ background:#e2e8f0; padding:2px 6px; border-radius:6px; }}
 .dim {{ color:#475569; }}
 a.btn {{ display:inline-block; padding:.5rem .75rem; border:1px solid #334155; border-radius:.5rem; text-decoration:none; }}
</style>
</head><body>
  <h1>개인정보 처리 안내</h1>
  <p class="dim">최종 갱신: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>

  <section>
    <h2>데이터 처리 원칙</h2>
    <ul>
      <li>요약 우선 저장(Summary-only): <strong>{yn(SAFE_SUMMARY_ONLY)}</strong></li>
      <li>원문 전체 저장(STORE_FULLTEXT): <strong>{yn(STORE_FULLTEXT)}</strong></li>
      <li>robots.txt 준수: <strong>{yn(RESPECT_ROBOTS)}</strong></li>
      <li>안전 모드(SAFE_MODE_ENABLED): <strong>{yn(SAFE_MODE_ENABLED)}</strong></li>
      <li>IP 해시 저장(LOG_IP_HASHED): <strong>{yn(LOG_IP_HASHED)}</strong></li>
      <li>로그 보관 기간(RETENTION_DAYS): <strong>{RETENTION_DAYS}일</strong></li>
    </ul>
    <p>본 서비스의 생성 모델: <code>{MODEL_NAME}</code></p>
  </section>

  <section>
    <h2>문의</h2>
    <p>정식 개인정보 처리방침(템플릿 기반)은 <code>/privacy</code> 에서 확인할 수 있습니다.</p>
    <p>
      {('<a href="%s" target="_blank" rel="noopener noreferrer" class="btn">자세히 보기</a>' % PRIVACY_PAGE_URL) if PRIVACY_PAGE_URL else '<span class="dim">별도 외부 링크가 설정되어 있지 않습니다.</span>'}
    </p>
  </section>
</body></html>"""
    resp = HttpResponse(html, content_type="text/html; charset=utf-8")
    resp["Cache-Control"] = "public, max-age=600"
    return resp


# ─────────────────────────────────────────────────────────────
# 헬스체크
# ─────────────────────────────────────────────────────────────
def healthz(_request: HttpRequest):
    return HttpResponse("ok", content_type="text/plain; charset=utf-8")


# ─────────────────────────────────────────────────────────────
# 동의 증빙 수집 (프런트 JS가 /legal/consent/confirm 로 POST)
# ─────────────────────────────────────────────────────────────
@require_http_methods(["POST"])
def consent_confirm(request: HttpRequest):
    """
    요청 JSON 예시:
      {
        "version":"v1","action":"accept","ts":"2025-11-02T12:34:56.789Z",
        "path":"/","ref":"https://example.com","tz":"Asia/Seoul","locale":"ko-KR",
        "ua":"...", "screen_w":1920,"screen_h":1080,
        "checkbox_checked":true
      }
    """
    try:
        raw = request.body.decode("utf-8") if request.body else "{}"
        payload = json.loads(raw or "{}")
    except Exception:
        return _json_fail("invalid json", status=400)

    action = str(payload.get("action") or "").lower()
    if action not in ("accept", "agree", "ok", "yes"):
        return _json_fail("action must be 'accept'", status=400)

    version = str(payload.get("version") or (getattr(settings, "CONSENT_VERSION", "v1")))
    client_ip = _client_ip(request)

    anonymize = getattr(settings, "ANONYMIZE_IP", True)
    hash_only = getattr(settings, "LOG_IP_HASHED", False)

    ip_plain: Optional[str] = None
    ip_hashed: Optional[str] = None

    if client_ip:
        if hash_only or anonymize:
            ip_hashed = _hash_ip(client_ip)
        else:
            ip_plain = client_ip

    evidence_id: Optional[int] = None
    stored_to = "fallback"

    try:
        from ragapp.models import ConsentEvidence  # type: ignore
        ce = ConsentEvidence.objects.create(
            version=version,
            consent_action=action,
            checkbox_checked=bool(payload.get("checkbox_checked", True)),
            page_path=str(payload.get("path") or request.path),
            referrer=str(payload.get("ref") or request.META.get("HTTP_REFERER", "")),
            user_agent=str(payload.get("ua") or request.META.get("HTTP_USER_AGENT", ""))[:512],
            client_ip=(None if (hash_only or anonymize) else (ip_plain or None)),
            ip_hashed=ip_hashed,
            ip_hash_salt_used=bool(getattr(settings, "LOG_IP_HASH_SECRET", "")),
            tz=str(payload.get("tz") or ""),
            locale=str(payload.get("locale") or ""),
            screen_w=payload.get("screen_w"),
            screen_h=payload.get("screen_h"),
            extra_json=payload,
        )
        evidence_id = ce.id
        stored_to = "ConsentEvidence"
    except Exception:
        try:
            from ragapp.models import MyLog  # type: ignore
            MyLog.objects.create(
                mode_text="consent",
                query=f"{version}:{action}",
                ok_flag=True,
                remote_addr_text=(ip_hashed or ip_plain or ""),
                extra_json={"fallback": True, "payload": payload},
            )
            stored_to = "MyLog"
        except Exception:
            stored_to = "none"

    return _json_ok({
        "id": evidence_id,
        "stored_to": stored_to,
        "server_ts": datetime.utcnow().isoformat() + "Z",
    })

# 기존 urls.py 호환을 위해 별칭 유지
consent_record = consent_confirm


__all__ = [
    # news_views 쪽 재노출
    "legal_privacy",
    "legal_tos",
    "legal_overseas",
    "legal_tester",
    "legal_guide",
    "legal_bundle",
    # 이 파일 고유 엔드포인트
    "robots_txt",
    "privacy_page",
    "healthz",
    "consent_confirm",
    "consent_record",
]
