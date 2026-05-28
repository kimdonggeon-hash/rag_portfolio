# ragapp/middleware/ops_control.py
from __future__ import annotations

import time
import json
import hashlib
from typing import Any, Dict, Tuple

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.db.utils import OperationalError, DatabaseError


# ---------------------------------------------------------------------
# Cache Keys (외부에서 import해서 쓰는 상수들)
# ---------------------------------------------------------------------
K_MAINT = "ops:maintenance"          # 0/1
K_WRITELOCK = "ops:writelock"        # 0/1
K_SPIKE = "ops:spike_guard"          # 0/1
K_UPDATED_AT = "ops:updated_at"      # 아무 값(갱신 트리거)

# presence index keys (views_ops_control.py에서 reset할 때 사용)
K_PRESENCE_INDEX_U = "presence:index:u"
K_PRESENCE_INDEX_A = "presence:index:a"


def _jsonish(request: HttpRequest) -> bool:
    p = request.path or ""
    if p.startswith("/api/"):
        return True
    accept = (request.META.get("HTTP_ACCEPT") or "").lower()
    return ("application/json" in accept) or ("text/json" in accept)


def _resp_block(request: HttpRequest, *, status: int, code: str, title: str, message: str) -> HttpResponse:
    if _jsonish(request):
        return JsonResponse(
            {"ok": False, "error": code, "title": title, "message": message},
            status=status,
            json_dumps_params={"ensure_ascii": False},
        )

    # 아주 단순 HTML(템플릿 의존 X) — 서비스가 깨져도 안내는 뜨게
    html = f"""<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title}</title>
<style>
  body{{margin:0;background:#020617;color:#e5e7eb;font-family:system-ui,-apple-system,"Noto Sans KR",sans-serif}}
  .wrap{{max-width:720px;margin:48px auto;padding:0 18px}}
  .card{{border:1px solid rgba(148,163,184,.25);border-radius:18px;background:rgba(15,23,42,.85);
        padding:18px;box-shadow:0 20px 60px rgba(0,0,0,.45)}}
  .t{{font-size:18px;font-weight:800;margin:0 0 8px}}
  .m{{color:rgba(229,231,235,.85);line-height:1.6;white-space:pre-line}}
  .btn{{display:inline-flex;align-items:center;justify-content:center;margin-top:14px;
        padding:10px 14px;border-radius:999px;border:1px solid rgba(148,163,184,.35);
        background:rgba(255,255,255,.06);color:#e5e7eb;text-decoration:none;font-weight:700}}
</style>
</head><body>
  <div class="wrap"><div class="card">
    <p class="t">{title}</p>
    <div class="m">{message}</div>
    <a class="btn" href="/">홈으로</a>
  </div></div>
</body></html>"""
    return HttpResponse(html, status=status, content_type="text/html; charset=utf-8")


def _bool_cache(key: str, default: int = 0) -> bool:
    v = cache.get(key, default)
    try:
        return bool(int(v))
    except Exception:
        return bool(v)


def _set_bool_cache(key: str, enabled: bool, ttl: int) -> None:
    cache.set(key, 1 if enabled else 0, timeout=ttl)
    cache.set(K_UPDATED_AT, str(int(time.time())), timeout=ttl)


def _safe_prefixes() -> Tuple[str, ...]:
    return tuple(getattr(settings, "OPS_CONTROL_SAFE_PREFIXES", (
        "/healthz",
        "/api/ping",
        "/robots.txt",
        "/favicon.ico",
        "/static/",
        "/uploads/",
        "/legal/",
        "/guide",
    )))


def _skip_presence_prefixes() -> Tuple[str, ...]:
    return tuple(getattr(settings, "OPS_CONTROL_PRESENCE_SKIP_PREFIXES", (
        "/static/",
        "/favicon.ico",
        "/robots.txt",
        "/healthz",
        "/api/ping",
        "/uploads/probe",
        "/admin/obsbadge/",          # 세션 토글 쪽은 카운트에서 제외
        "/ragadmin/ops/api/",        # ops 폴링이 카운트를 부풀리지 않게
    )))


def _presence_window_sec() -> int:
    return int(getattr(settings, "OPS_CONTROL_PRESENCE_TTL_SECONDS", 90))


def _presence_index_max() -> int:
    return int(getattr(settings, "OPS_CONTROL_PRESENCE_INDEX_MAX", 3000))


def _fingerprint(request: HttpRequest) -> str:
    # 세션이 있으면 세션을 우선(가장 “사람”에 가까움)
    try:
        sk = getattr(getattr(request, "session", None), "session_key", None)
        if sk:
            return f"s:{sk}"
    except Exception:
        pass

    ip = (request.META.get("HTTP_X_FORWARDED_FOR") or request.META.get("REMOTE_ADDR") or "").split(",")[0].strip()
    ua = (request.META.get("HTTP_USER_AGENT") or "")[:200]
    raw = f"{ip}|{ua}"
    h = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"h:{h}"


def _presence_touch(kind: str, fid: str) -> None:
    now = int(time.time())
    ttl = _presence_window_sec()
    maxn = _presence_index_max()

    idx_key = K_PRESENCE_INDEX_A if kind == "a" else K_PRESENCE_INDEX_U

    # index는 dict(fid -> last_seen_epoch)
    try:
        idx = cache.get(idx_key) or {}
        if not isinstance(idx, dict):
            idx = {}
    except Exception:
        idx = {}

    idx[str(fid)] = now

    # prune
    cutoff = now - ttl
    if idx:
        # 오래된 것 제거
        dead = [k for k, ts in idx.items() if not isinstance(ts, int) or ts < cutoff]
        for k in dead:
            idx.pop(k, None)

    # cap
    if len(idx) > maxn:
        items = sorted(((k, v) for k, v in idx.items() if isinstance(v, int)), key=lambda kv: kv[1], reverse=True)
        idx = dict(items[:maxn])

    # index는 오래 유지(리셋 용이), 실제 “활성”은 ttl 컷오프로 판단
    try:
        cache.set(idx_key, idx, timeout=3600 * 24 * 30)
    except Exception:
        pass

    # 개별 키는 ttl(선택) — 없어도 index로 충분하지만, 디버깅용으로 남김
    try:
        cache.set(f"presence:{kind}:{fid}", now, timeout=ttl + 5)
    except Exception:
        pass


def _presence_snapshot() -> Dict[str, Any]:
    now = int(time.time())
    ttl = _presence_window_sec()
    cutoff = now - ttl

    def _count(idx_key: str) -> int:
        try:
            idx = cache.get(idx_key) or {}
            if not isinstance(idx, dict):
                return 0
        except Exception:
            return 0

        c = 0
        for _k, ts in idx.items():
            if isinstance(ts, int) and ts >= cutoff:
                c += 1
        return c

    users = _count(K_PRESENCE_INDEX_U)
    admins = _count(K_PRESENCE_INDEX_A)
    return {"users": users, "admins": admins, "total": users + admins, "window_sec": ttl}


def _spike_params() -> Tuple[int, int]:
    # window / limit (전역)
    win = int(getattr(settings, "OPS_CONTROL_SPIKE_WINDOW_SECONDS", 5))
    lim = int(getattr(settings, "OPS_CONTROL_SPIKE_LIMIT", 200))
    return max(1, win), max(10, lim)


def _spike_hit() -> bool:
    # True = 허용, False = 차단
    win, lim = _spike_params()
    bucket = int(time.time() // win)
    ck = f"ops:spike:bucket:{bucket}"
    ttl = win + 2
    try:
        cache.add(ck, 0, timeout=ttl)
        n = cache.incr(ck)
    except Exception:
        cache.set(ck, 1, timeout=ttl)
        n = 1
    return int(n) <= int(lim)


class OpsControlMiddleware:
    """
    ✅ 운영 컨트롤(점검/쓰기잠금/스파이크) + 접속자 수(최근 N초)

    - 점검(maintenance): 일반 사용자에게 503 안내 (관리자는 통과)
    - 쓰기잠금(writelock): 일반 사용자 unsafe method(POST/PUT/PATCH/DELETE) 423 안내
    - 스파이크(spike_guard): ON이면 전역 요청 폭주 시 429로 완충 (관리자는 통과)
    - 접속자수: cache index 기반 (Cloud Run 다중 인스턴스면 Redis 캐시 권장)
    """

    @staticmethod
    def snapshot_ops() -> Dict[str, Any]:
        return {
            "maintenance": 1 if _bool_cache(K_MAINT, 0) else 0,
            "writelock": 1 if _bool_cache(K_WRITELOCK, 0) else 0,
            "spike_guard": 1 if _bool_cache(K_SPIKE, 0) else 0,
            "updated_at": cache.get(K_UPDATED_AT, "") or "",
        }

    @staticmethod
    def snapshot_online() -> Dict[str, Any]:
        return _presence_snapshot()

    def __init__(self, get_response):
        self.get_response = get_response
        self.safe_prefixes = _safe_prefixes()
        self.skip_presence_prefixes = _skip_presence_prefixes()
        self.ops_ttl = int(getattr(settings, "OPS_CONTROL_TTL_SECONDS", 3600 * 24 * 30))

    def __call__(self, request: HttpRequest) -> HttpResponse:
        path = request.path or "/"
        method = (request.method or "GET").upper()

        if any(path.startswith(p) for p in ("/static/", "/favicon.ico", "/robots.txt", "/healthz", "/api/ping")):
            return self.get_response(request)

        # user 판별(관리자 무제한)
        try:
            user = getattr(request, "user", None)
            is_admin = bool(
                getattr(user, "is_authenticated", False) and getattr(user, "is_staff", False)
            )
        except (OperationalError, DatabaseError):
            user = None
            is_admin = False

        # -----------------------------------------------------------------
        # Presence touch (ops/api 폴링 등은 제외)
        # -----------------------------------------------------------------
        if not any(path.startswith(p) for p in self.skip_presence_prefixes):
            try:
                kind = "a" if is_admin else "u"
                _presence_touch(kind, _fingerprint(request))
            except Exception:
                pass

        # -----------------------------------------------------------------
        # 운영 토글 적용 (관리자는 통과)
        # -----------------------------------------------------------------
        if not is_admin:
            # 1) maintenance
            if _bool_cache(K_MAINT, 0):
                # safe prefixes는 통과
                if not any(path.startswith(p) for p in self.safe_prefixes):
                    resp = _resp_block(
                        request,
                        status=503,
                        code="maintenance",
                        title="점검 중입니다",
                        message="잠시만요! 운영 점검이 진행 중이에요.\n잠시 뒤 다시 접속해 주세요.",
                    )
                    try:
                        resp["Retry-After"] = "60"
                    except Exception:
                        pass
                    return resp

            # 2) writelock (쓰기 요청 차단)
            if _bool_cache(K_WRITELOCK, 0) and method not in ("GET", "HEAD", "OPTIONS", "TRACE"):
                return _resp_block(
                    request,
                    status=423,
                    code="writelock",
                    title="쓰기 잠금 상태입니다",
                    message="현재 운영 보호를 위해 저장/업로드/등록 요청이 잠시 막혀 있어요.\n잠시 뒤 다시 시도해 주세요.",
                )

            # 3) spike guard (전역 폭주 완충)
            if _bool_cache(K_SPIKE, 0):
                if not _spike_hit():
                    return _resp_block(
                        request,
                        status=429,
                        code="spike_guard",
                        title="요청이 잠시 몰리고 있어요",
                        message="지금은 요청이 몰려서 잠시 쉬어갈게요.\n조금만 기다렸다가 다시 시도해 주세요.",
                    )

        # pass through
        return self.get_response(request)
