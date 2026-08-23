# ragapp/middleware/request_guard.py
from __future__ import annotations

import time
import re
import uuid
from typing import Dict, Optional, Tuple, Iterable, List
from urllib.parse import urlparse

from django.conf import settings
from django.core.cache import cache
from django.http import (
    HttpRequest,
    HttpResponse,
    JsonResponse,
    HttpResponseNotFound,
)
from django.utils import timezone
from django.shortcuts import render

from ragapp.services.usage_limiter import (
    build_client_key,
    is_admin_unlimited,
    get_cookie_cid,
    CID_COOKIE_NAME,
)

# (있으면) DailyUsage 모델 사용
try:
    from ragapp.models import DailyUsage  # type: ignore
except Exception:  # pragma: no cover
    DailyUsage = None  # type: ignore


# ----------------------------
# JSON 요청 판별 (API + Accept)
# ----------------------------
def _is_jsonish_request(request: HttpRequest) -> bool:
    path = request.path or ""
    if path.startswith("/api/"):
        return True
    accept = (request.META.get("HTTP_ACCEPT") or "").lower()
    return "application/json" in accept or "text/json" in accept


# ----------------------------
# “예쁜” 차단 페이지 (웹) + JSON (API)
# ----------------------------
def _guard_page(
    request: HttpRequest,
    *,
    status: int,
    code: str,
    title: str,
    message: str,
    meta=None,
) -> HttpResponse:
    if _is_jsonish_request(request):
        return JsonResponse(
            {"ok": False, "error": code, "message": message},
            status=status,
            json_dumps_params={"ensure_ascii": False},
        )

    back = request.META.get("HTTP_REFERER") or "/"
    try:
        return render(
            request,
            "ragapp/guard_blocked.html",
            {
                "status": status,
                "code": code,
                "title": title,
                "message": message,
                "meta": meta or [],
                "back_url": back,
            },
            status=status,
        )
    except Exception:
        return HttpResponse(
            f"{title}\n\n{message}",
            status=status,
            content_type="text/plain; charset=utf-8",
        )


# ----------------------------
# Cache 기반 레이트리밋
# ----------------------------
def _rate_hit(key: str, window_sec: int, limit: int) -> Tuple[bool, int]:
    """
    window_sec 동안 limit회 허용. (ok, count)
    - cache(LocMem/Redis) 필요
    """
    bucket = int(time.time() // max(1, window_sec))
    ck = f"rl:{key}:{bucket}"
    ttl = window_sec + 2

    try:
        cache.add(ck, 0, timeout=ttl)
        n = cache.incr(ck)
    except Exception:
        cache.set(ck, 1, timeout=ttl)
        n = 1

    return (int(n) <= int(limit)), int(n)


# ----------------------------
# Origin/Referer 동일 출처 확인 (와일드카드 포함)
# ----------------------------
def _split_trusted_origins(raw: object) -> List[str]:
    """
    settings.CSRF_TRUSTED_ORIGINS가 list/tuple이든 콤마 문자열이든 안전하게 정규화.
    """
    out: List[str] = []
    if raw is None:
        return out

    if isinstance(raw, (list, tuple, set)):
        items: Iterable[object] = raw
    else:
        items = [raw]

    for it in items:
        if it is None:
            continue
        s = str(it).strip()
        if not s:
            continue
        # "a,b,c" 형태면 split
        if "," in s:
            for part in s.split(","):
                p = part.strip()
                if p:
                    out.append(p)
        else:
            out.append(s)
    return out


def _origin_from_url(u: str) -> Optional[str]:
    """
    URL/Origin 문자열에서 scheme://netloc 형태의 origin만 뽑아냄.
    - Referer는 path/query가 붙어도 origin만 추출
    """
    try:
        s = (u or "").strip()
        if not s:
            return None
        p = urlparse(s)
        if not p.scheme or not p.netloc:
            return None
        return f"{p.scheme.lower()}://{p.netloc.lower()}"
    except Exception:
        return None


def _match_trusted_origin(candidate_origin: str, trusted: Iterable[str]) -> bool:
    """
    candidate_origin: "https://host[:port]" (소문자 정규화된 문자열)
    trusted: settings.CSRF_TRUSTED_ORIGINS + my_origin 등

    지원:
    - 정확 일치: https://donggeonproject.co.kr
    - 와일드카드 서브도메인: https://*.run.app  (scheme 일치 + host가 *. 뒤 도메인으로 끝나면 OK)
    - 단순 prefix로 과하게 열지 않도록 URL 파싱 기반으로 비교
    """
    co = _origin_from_url(candidate_origin)
    if not co:
        return False

    cp = urlparse(co)
    c_scheme = (cp.scheme or "").lower()
    c_hostport = (cp.netloc or "").lower()

    # host / port 분리
    if ":" in c_hostport:
        c_host, c_port = c_hostport.rsplit(":", 1)
    else:
        c_host, c_port = c_hostport, ""

    for t in trusted:
        ts = (str(t or "").strip())
        if not ts:
            continue

        # trusted가 "https://*.run.app" 형태를 기대하지만, 혹시 scheme 없이 들어오면 안전하게 보정
        if "://" not in ts:
            ts = "https://" + ts

        to = _origin_from_url(ts)
        if not to:
            continue

        tp = urlparse(to)
        t_scheme = (tp.scheme or "").lower()
        t_hostport = (tp.netloc or "").lower()

        if t_scheme and c_scheme and t_scheme != c_scheme:
            continue

        if ":" in t_hostport:
            t_host, t_port = t_hostport.rsplit(":", 1)
        else:
            t_host, t_port = t_hostport, ""

        # 포트가 명시된 trusted는 포트까지 일치 요구
        if t_port:
            if not c_port or c_port != t_port:
                continue

        # 1) 정확 host 매치
        if t_host == c_host:
            return True

        # 2) 와일드카드 서브도메인 매치: "*.example.com"
        if t_host.startswith("*."):
            base = t_host[2:]  # example.com
            if not base:
                continue
            # 보수적으로: base 자체도 허용하려면 (c_host == base) 추가
            if c_host == base:
                return True
            if c_host.endswith("." + base):
                return True

    return False


def _same_origin_ok(request: HttpRequest) -> bool:
    """
    unsafe method에서:
    - Origin 있으면 origin이 same-origin / CSRF_TRUSTED_ORIGINS 포함이어야 함
    - Origin 없으면 Referer의 origin이 same-origin / CSRF_TRUSTED_ORIGINS 포함이어야 함
    """
    # request.is_secure() 대신 request.scheme 사용 (프록시 환경에서 SECURE_PROXY_SSL_HEADER 적용 가능)
    host = (request.get_host() or "").strip()
    scheme = (getattr(request, "scheme", "") or "http").lower()
    my_origin = f"{scheme}://{host}".lower()

    raw_trusted = getattr(settings, "CSRF_TRUSTED_ORIGINS", []) or []
    trusted_list = _split_trusted_origins(raw_trusted)

    # 항상 자기 자신(origin)은 허용
    trusted_list.append(my_origin)

    origin = (request.META.get("HTTP_ORIGIN") or "").strip()
    referer = (request.META.get("HTTP_REFERER") or "").strip()

    cand = _origin_from_url(origin) if origin else None
    if not cand and referer:
        cand = _origin_from_url(referer)

    if not cand:
        # 둘 다 없으면 하드 모드 차단
        return False

    # 최종 판정
    return _match_trusted_origin(cand, trusted_list)


# ----------------------------
# DailyUsage(일일 쿼터) 유틸 (선택적으로만 사용)
# ----------------------------
def _model_fields(model) -> set[str]:
    try:
        return {f.name for f in model._meta.get_fields()}  # type: ignore[attr-defined]
    except Exception:
        return set()


def _limits() -> Dict[str, int]:
    return {
        "image": int(getattr(settings, "QA_USAGE_LIMIT_IMAGE", 3)),
        "table": int(getattr(settings, "QA_USAGE_LIMIT_TABLE", 3)),
    }


def _kind_to_field(fields: set[str]) -> Dict[str, str]:
    cand = {
        "image": ["image_used", "image", "image_count"],
        "table": ["table_used", "table", "table_count"],
    }
    out: Dict[str, str] = {}
    for kind, names in cand.items():
        for nm in names:
            if nm in fields:
                out[kind] = nm
                break
    return out


def _consume_daily(
    request: HttpRequest,
    kind: str,
    amount: int = 1,
) -> Tuple[bool, Optional[int], Optional[int]]:
    """
    kind(image/table) 일일 쿼터 1회 소모.
    반환: (ok, remaining_after or None(무제한/미집계), limit or None)

    ⚠️ 주의:
    - 업로드 POST에서 이 로직을 쓰면 UsageQuotaMiddleware와 중복 차감 가능
    - 그래서 RequestGuardMiddleware에서 enable 스위치로 완전히 끌 수 있게 해둠.
    """
    if DailyUsage is None:
        return True, None, None

    limits = _limits()
    limit = int(limits.get(kind, 0))

    # limit <= 0 => 무제한
    if limit <= 0:
        return True, None, None

    key = build_client_key(request)
    today = timezone.localdate()

    fields = _model_fields(DailyUsage)
    k2f = _kind_to_field(fields)
    field = k2f.get(kind)
    if not field:
        # 모델 필드 불일치면 서비스 안 깨지게 허용
        return True, None, limit

    date_field = "date" if "date" in fields else ("day" if "day" in fields else "date")
    key_field = "client_key" if "client_key" in fields else ("key" if "key" in fields else "client_key")

    qs = DailyUsage.objects  # type: ignore[attr-defined]
    try:
        obj = qs.filter(**{date_field: today, key_field: key}).first()
        if obj is None:
            obj = DailyUsage(**{date_field: today, key_field: key})
    except Exception:
        return True, None, limit

    try:
        used = int(getattr(obj, field, 0) or 0)
    except Exception:
        used = 0

    if used + int(amount) > limit:
        return False, 0, limit

    try:
        setattr(obj, field, used + int(amount))
        if "updated_at" in fields:
            setattr(obj, "updated_at", timezone.now())
        obj.save()
    except Exception:
        return True, None, limit

    remaining = max(0, limit - (used + int(amount)))
    return True, remaining, limit


# ----------------------------
# UA(모바일) 판별
# ----------------------------
_DEFAULT_MOBILE_UA_RE = re.compile(
    r"(Mobile|Android|iPhone|iPad|iPod|IEMobile|BlackBerry|Opera Mini)",
    re.IGNORECASE,
)


class RequestGuardMiddleware:
    """
    ✅ 한 파일에 “하드 보안 패키지” 묶음

    1) path denylist (+ 선택 allowlist)
    2) 관리자 페이지 PC only
    3) unsafe method Origin/Referer same-origin 강제
    4) (옵션) 업로드 일일 쿼터(image/table) - admin 무제한
       - ⚠️ UsageQuotaMiddleware와 같이 켜면 중복 차감 가능
       - REQUEST_GUARD_ENABLE_DAILY_UPLOAD_QUOTA=False 권장(기본값도 False)
    5) IP/UA 해시 기반 레이트리밋
    6) 보안 헤더 + CSP
    """

    def __init__(self, get_response):
        self.get_response = get_response

        # 스캐너 컷(무조건 404)
        self.deny_prefixes = tuple(
            getattr(
                settings,
                "REQUEST_GUARD_DENY_PREFIXES",
                [
                    "/.env",
                    "/.git",
                    "/.svn",
                    "/.hg",
                    "/wp-admin",
                    "/wordpress",
                    "/phpmyadmin",
                    "/pma",
                    "/vendor/phpunit",
                    "/cgi-bin",
                    "/server-status",
                ],
            )
        )

        # (선택) allowlist를 켜면, 목록 밖 path는 404
        self.strict_allowlist = bool(getattr(settings, "REQUEST_GUARD_STRICT_ALLOWLIST", False))
        self.allow_prefixes = tuple(
            getattr(
                settings,
                "REQUEST_GUARD_ALLOW_PREFIXES",
                [
                    "/",          # ✅ "/"는 “루트만” 허용(아래 _in_allowlist에서 특수 처리)
                    "/api/",
                    "/static/",
                    "/uploads/",
                    "/media/",
                    "/table/",
                    "/ragadmin/",
                    "/admin/",
                    "/legal/",
                ],
            )
        )

        # 관리자 경로 + PC only
        self.admin_prefixes = tuple(getattr(settings, "REQUEST_GUARD_ADMIN_PREFIXES", ["/admin", "/ragadmin"]))
        self.admin_pc_only = bool(getattr(settings, "REQUEST_GUARD_ADMIN_PC_ONLY", True))
        self.mobile_ua_re = getattr(settings, "REQUEST_GUARD_MOBILE_UA_RE", None) or _DEFAULT_MOBILE_UA_RE

        # ✅ 관리자 로그인/로그아웃은 익명도 접근 가능 예외 허용
        self.admin_anon_allow = tuple(
            getattr(
                settings,
                "REQUEST_GUARD_ADMIN_ANON_ALLOW",
                [
                    "/admin/login",
                    "/admin/logout",
                    "/ragadmin/login",
                    "/ragadmin/logout",
                ],
            )
        )

        # Origin/Referer 검사 스위치
        self.origin_check = bool(getattr(settings, "REQUEST_GUARD_ORIGIN_CHECK", True))

        # ✅ 업로드 일일 쿼터 기능 스위치 (중복 차감 방지용)
        # - 기본값 False (UsageQuotaMiddleware가 업로드 쿼터를 담당하는 “단일화”가 안전)
        self.enable_daily_upload_quota = bool(
            getattr(settings, "REQUEST_GUARD_ENABLE_DAILY_UPLOAD_QUOTA", False)
        )

        # 업로드 쿼터 적용(POST만) - enable일 때만
        if self.enable_daily_upload_quota:
            self.daily_quota_upload_map: Dict[str, str] = dict(
                getattr(
                    settings,
                    "REQUEST_GUARD_DAILY_QUOTA_UPLOAD_MAP",
                    {
                        "/media/index": "image",
                        "/media/upload": "image",
                        "/table/index": "table",
                        "/table/upload": "table",
                    },
                )
            )
        else:
            self.daily_quota_upload_map = {}

        # 레이트리밋 규칙 (prefix, window_sec, limit)
        self.rate_rules = list(
            getattr(
                settings,
                "REQUEST_GUARD_RATE_RULES",
                [
                    ("/api/usage/status/", 30, 20),
                    ("/media/search", 60, 60),
                    ("/table/search", 60, 60),
                ],
            )
        )

        self.security_headers = {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "same-origin",
            "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
        }

        self.csp_value = getattr(
            settings,
            "REQUEST_GUARD_CSP",
            (
                "default-src 'self'; "
                "img-src 'self' data: blob: https:; "
                "style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; "
                "connect-src 'self' https: ws: wss:; "
                "frame-ancestors 'none'; "
                "base-uri 'self'; "
                "form-action 'self'"
            ),
        )

    def _match_rate_rule(self, path: str) -> Optional[Tuple[int, int]]:
        for prefix, window_sec, limit in self.rate_rules:
            if path.startswith(prefix):
                return int(window_sec), int(limit)
        return None

    def _in_allowlist(self, path: str) -> bool:
        # ✅ "/"는 prefix로 두면 전부 허용이 되어버리므로 “루트만” 정확히 매칭
        for p in self.allow_prefixes:
            if p == "/":
                if path == "/":
                    return True
                continue
            if path.startswith(p):
                return True
        return False

    def _is_admin_path(self, path: str) -> bool:
        return any(path.startswith(p) for p in self.admin_prefixes)

    def _is_admin_anon_allowed(self, path: str) -> bool:
        # Let Django's admin index redirect anonymous visitors to its login
        # page. Other admin routes remain hidden unless explicitly allowed.
        if path.rstrip("/") in {"/admin", "/ragadmin"}:
            return True
        return any(path.startswith(p) for p in self.admin_anon_allow)

    def _is_mobile_ua(self, request: HttpRequest) -> bool:
        ua = request.META.get("HTTP_USER_AGENT") or ""
        try:
            return bool(self.mobile_ua_re.search(ua))
        except Exception:
            return False

    def _daily_quota_kind(self, request: HttpRequest) -> Optional[str]:
        if request.method.upper() != "POST":
            return None
        path = request.path or "/"
        for prefix, kind in self.daily_quota_upload_map.items():
            if path.startswith(prefix):
                return str(kind)
        return None

    def __call__(self, request: HttpRequest) -> HttpResponse:
        path = request.path or "/"
        method = request.method.upper()

        # 1) denylist: 즉시 404
        for p in self.deny_prefixes:
            if path.startswith(p):
                return HttpResponseNotFound("Not Found")

        # 2) (선택) allowlist
        if self.strict_allowlist and not self._in_allowlist(path):
            return HttpResponseNotFound("Not Found")

        # 3) 관리자 페이지: 비관리자 은닉(404) + PC only (403)
        if self._is_admin_path(path):
            user = getattr(request, "user", None)
            is_staff = bool(getattr(user, "is_authenticated", False) and getattr(user, "is_staff", False))
            is_su = bool(getattr(user, "is_authenticated", False) and getattr(user, "is_superuser", False))

            if not (is_staff or is_su):
                if not self._is_admin_anon_allowed(path):
                    return HttpResponseNotFound("Not Found")

            if self.admin_pc_only and self._is_mobile_ua(request):
                return _guard_page(
                    request,
                    status=403,
                    code="admin_pc_only",
                    title="관리자 페이지는 PC 전용이에요",
                    message="보안상 관리자 페이지는 PC에서만 접근할 수 있습니다.",
                    meta=["정책: PC only", "대상: /admin, /ragadmin"],
                )

        # 4) Origin/Referer 검사 (unsafe methods)
        if self.origin_check and method not in ("GET", "HEAD", "OPTIONS", "TRACE"):
            if not _same_origin_ok(request):
                return _guard_page(
                    request,
                    status=403,
                    code="bad_origin",
                    title="요청 출처가 올바르지 않아요",
                    message="요청 헤더(Origin/Referer)가 허용된 출처가 아닙니다.",
                    meta=["정책: same-origin", "POST 보호"],
                )

        # 5) (옵션) 업로드 일일 쿼터(image/table) - admin 무제한
        #    ※ '요청 시도' 기준 1회 소모
        #    ⚠️ UsageQuotaMiddleware에서도 업로드 POST에서 차감하면 중복 차감됨
        if self.enable_daily_upload_quota and (not is_admin_unlimited(request)):
            kind = self._daily_quota_kind(request)
            if kind in ("image", "table"):
                ok, _remain, limit = _consume_daily(request, kind, 1)
                if not ok:
                    limit = int(limit or _limits().get(kind, 0))
                    label = "이미지 업로드" if kind == "image" else "표 업로드"
                    return _guard_page(
                        request,
                        status=429,
                        code="daily_quota_exceeded",
                        title="오늘 업로드 횟수를 다 썼어요",
                        message=f"오늘 {label}는 {limit}회까지 가능합니다.\n내일 다시 시도해 주세요.",
                        meta=[f"종류: {kind}", f"일일 제한: {limit}회", "대상: 일반 사용자"],
                    )

        # 6) 레이트리밋 - admin 무제한
        if not is_admin_unlimited(request):
            rule = self._match_rate_rule(path)
            if rule:
                window_sec, limit = rule
                key = build_client_key(request)
                ok, n = _rate_hit(f"{key}:{path}", window_sec, limit)
                if not ok:
                    return _guard_page(
                        request,
                        status=429,
                        code="rate_limited",
                        title="요청이 너무 빠르게 들어오고 있어요",
                        message="잠시만 기다렸다가 다시 시도해 주세요.",
                        meta=[f"경로: {path}", f"윈도우: {window_sec}s", f"한도: {limit}회", f"카운트: {n}"],
                    )

        # ---- 실제 처리 ----
        resp = self.get_response(request)

        # 7) 보안 헤더: 이미 설정되어 있으면 덮어쓰지 않음
        try:
            for k, v in self.security_headers.items():
                if k not in resp:
                    resp[k] = v
            if "Content-Security-Policy" not in resp and self.csp_value:
                resp["Content-Security-Policy"] = self.csp_value
        except Exception:
            pass

        # 8) 기기 식별 쿠키(dg_cid) 발급: 없으면 새로 심어서 다음 요청부터
        #    IP가 바뀌어도(공유기 재시작 등) 오늘 사용량이 유지되게 한다.
        try:
            if not get_cookie_cid(request):
                resp.set_cookie(
                    CID_COOKIE_NAME,
                    uuid.uuid4().hex,
                    max_age=60 * 60 * 24 * 730,  # 2년
                    httponly=True,
                    secure=bool(getattr(settings, "SESSION_COOKIE_SECURE", not settings.DEBUG)),
                    samesite="Lax",
                )
        except Exception:
            pass

        return resp
