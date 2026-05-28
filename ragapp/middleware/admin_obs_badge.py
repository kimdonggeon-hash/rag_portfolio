# ragapp/middleware/admin_obs_badge.py

from __future__ import annotations

import html
import json
import os
import time
from contextlib import ExitStack

from django.conf import settings
from django.db import connections
from django.templatetags.static import static
from django.template.loader import render_to_string

from ragapp.obs.spans import reset as spans_reset
from ragapp.obs.spans import set_active as spans_set_active
from ragapp.obs.spans import snapshot as spans_snapshot


class AdminObservabilityBadgeMiddleware:
    """
    ✅ 목표
    - "기록(최근 요청/DB/외부HTTP/Vertex)"은 enabled 세션이면 전 경로에서 누적
    - "배지 UI 주입"은 admin/ops/console/ragadmin 계열 HTML 응답에서만 수행

    주입 안전장치:
    - Content-Encoding(gzip/br/deflate 등) 된 응답은 주입 스킵(깨짐 방지)
    - HTML(text/html) + non-streaming + </body> 존재할 때만 주입
    """

    # ✅ UI 주입 대상(관리자 화면) 경로
    ADMIN_PATH_PREFIXES = ("/admin/", "/ops/", "/console/", "/ragadmin/")

    # ✅ 기록/주입 모두에서 제외(잡음/성능)
    IGNORE_PATH_PREFIXES = (
        "/static/",
        "/media/",
        "/favicon.ico",
        "/robots.txt",
        "/health",
        "/ready",
        "/metrics",
    )

    SESSION_ENABLED = "dg_obs_enabled"
    SESSION_CFG = "dg_obs_cfg"
    SESSION_HISTORY = "dg_obs_history"

    DEFAULT_CFG = {
        "pos": "br",     # br/bl/tr/tl
        "compact": 0,    # 0/1
        "history_max": 10,
        "fields": {
            "env": 1,
            "rev": 1,
            "host": 1,
            "rid": 1,
            "status": 1,
            "latency": 1,
            "db": 1,
            "ext": 1,      # External HTTP total
            "vertex": 1,   # Vertex subset
        },
    }

    def __init__(self, get_response):
        self.get_response = get_response

    # -------------------------
    # Path helpers
    # -------------------------
    def _is_admin_path(self, path: str) -> bool:
        return bool(path) and path.startswith(self.ADMIN_PATH_PREFIXES)

    def _should_ignore_path(self, path: str) -> bool:
        return bool(path) and path.startswith(self.IGNORE_PATH_PREFIXES)

    def __call__(self, request):
        # ✅ 컨텍스트 누수 방지: 모든 요청에서 비활성+reset
        spans_set_active(False)
        spans_reset()

        if not getattr(settings, "ADMIN_OBS_BADGE_ALLOWED", True):
            return self.get_response(request)

        path = request.path or ""
        if self._should_ignore_path(path):
            return self.get_response(request)

        # 사용자/권한 판별 (주입과 기록 제한에 사용)
        user = getattr(request, "user", None)
        is_auth = bool(getattr(user, "is_authenticated", False))
        is_staff_user = bool(
            is_auth and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))
        )

        # 세션 기반 on/off
        enabled = bool(request.session.get(self.SESSION_ENABLED, False))
        cfg = self._merge_cfg(request.session.get(self.SESSION_CFG))

        # ✅ 보안 기본값: staff만 기록 허용 (원하면 settings로 완화 가능)
        staff_only = bool(getattr(settings, "ADMIN_OBS_BADGE_STAFF_ONLY", True))
        if staff_only and enabled and not is_staff_user:
            enabled = False

        is_admin_path = self._is_admin_path(path)

        # ✅ (성능) enabled도 아니고 admin 화면도 아니면, 아무 것도 할 필요 없음
        if not enabled and not is_admin_path:
            return self.get_response(request)

        # ✅ enabled일 때만 Span 기록
        spans_set_active(enabled)
        spans_reset()

        t0 = time.perf_counter()
        db_time_ms = 0.0
        db_count = 0

        with ExitStack() as stack:
            if enabled:
                for alias in connections:
                    conn = connections[alias]
                    if hasattr(conn, "execute_wrapper"):

                        def _wrapper(execute, sql, params, many, context):
                            nonlocal db_time_ms, db_count
                            s = time.perf_counter()
                            try:
                                return execute(sql, params, many, context)
                            finally:
                                db_count += 1
                                db_time_ms += (time.perf_counter() - s) * 1000.0

                        stack.enter_context(conn.execute_wrapper(_wrapper))

            response = self.get_response(request)

        total_ms = (time.perf_counter() - t0) * 1000.0

        # ✅ Span snapshot (외부 HTTP/Vertex) — enabled가 아닐 때는 0으로
        span = spans_snapshot() if enabled else {}
        ext_total_ms = float(span.get("total_ms") or 0.0)
        ext_total_n = int(span.get("total_n") or 0)
        vertex_ms = float(span.get("vertex_ms") or 0.0)
        vertex_n = int(span.get("vertex_n") or 0)

        # ✅ 요청 종료 시점에 비활성화(누수 방지)
        spans_set_active(False)
        spans_reset()

        status_code = int(getattr(response, "status_code", 0))

        # ---------------------------------------------------------
        # ✅ (핵심) history 저장은 "주입 여부/응답 타입/압축"과 무관하게 먼저 수행
        # ---------------------------------------------------------
        history = request.session.get(self.SESSION_HISTORY) or []
        if enabled:
            rid_raw = getattr(request, "request_id", "") or request.headers.get("X-Request-ID", "")
            if not rid_raw:
                rid_raw = f"req-{int(time.time()*1000)}"

            item = {
                "ts": int(time.time()),
                "path": (request.path or "")[:160],
                "status": status_code,
                "ms": round(total_ms, 1),
                "db_ms": round(db_time_ms, 1),
                "db_q": int(db_count),
                "ext_ms": round(ext_total_ms, 1),
                "ext_n": int(ext_total_n),
                "vx_ms": round(vertex_ms, 1),
                "vx_n": int(vertex_n),
                "rid": str(rid_raw)[:64],
                "method": (getattr(request, "method", "") or "")[:10],
            }
            history.insert(0, item)
            history = history[: int(cfg.get("history_max", 10) or 10)]
            request.session[self.SESSION_HISTORY] = history
            request.session.modified = True

        # ---------------------------------------------------------
        # ✅ UI 주입은 "admin 경로 + staff" 에서만
        # ---------------------------------------------------------
        if not is_admin_path:
            return response
        if not is_staff_user:
            return response

        # ✅ 압축된 응답이면 주입 스킵 (미들웨어 순서 사고 방지)
        enc = (response.get("Content-Encoding") or "").lower()
        if enc and enc != "identity":
            return response

        ctype = (response.get("Content-Type") or "").lower()
        if "text/html" not in ctype:
            return response
        if getattr(response, "streaming", False):
            return response

        body = getattr(response, "content", None)
        if not body:
            return response

        try:
            text = body.decode(response.charset or "utf-8", errors="replace")
        except Exception:
            return response

        # ✅ (핵심) 중복 주입 방지
        low = text.lower()
        if 'id="dgobsroot"' in low or "admin obs (injected)" in low:
            return response

        idx = low.rfind("</body>")
        if idx < 0:
            return response

        # 아래 값들은 "주입"에서만 필요하므로 여기서 계산
        rid_raw = getattr(request, "request_id", "") or request.headers.get("X-Request-ID", "")
        if not rid_raw:
            rid_raw = f"req-{int(time.time()*1000)}"
        rid = html.escape(str(rid_raw))

        is_error = status_code >= 400

        env = getattr(settings, "ENV_NAME", "") or ("prod" if not settings.DEBUG else "dev")
        env = html.escape(str(env))

        rev = (getattr(settings, "K_REVISION", "") or os.environ.get("K_REVISION", "")) or ""
        rev = html.escape(str(rev)) if rev else ""

        host = os.environ.get("HOSTNAME", "") or ""
        host = html.escape(str(host)) if host else ""

        inject = self._render_ui(
            enabled=enabled,
            cfg=cfg,
            env=env,
            rev=rev,
            host=host,
            rid=rid,
            status_code=status_code,
            is_error=is_error,
            total_ms=total_ms,
            db_time_ms=db_time_ms,
            db_count=db_count,
            ext_total_ms=ext_total_ms,
            ext_total_n=ext_total_n,
            vertex_ms=vertex_ms,
            vertex_n=vertex_n,
            history=history,
        )

        new_text = text[:idx] + inject + text[idx:]
        response.content = new_text.encode(response.charset or "utf-8")
        if response.has_header("Content-Length"):
            response["Content-Length"] = str(len(response.content))
        return response

    def _merge_cfg(self, raw):
        cfg = json.loads(json.dumps(self.DEFAULT_CFG))
        if isinstance(raw, dict):
            cfg["pos"] = raw.get("pos", cfg["pos"])
            try:
                cfg["compact"] = 1 if int(raw.get("compact", cfg["compact"])) == 1 else 0
            except Exception:
                cfg["compact"] = 1 if bool(raw.get("compact", cfg["compact"])) else 0

            try:
                cfg["history_max"] = int(raw.get("history_max", cfg["history_max"]) or cfg["history_max"])
                cfg["history_max"] = max(3, min(30, cfg["history_max"]))
            except Exception:
                pass

            fields = dict(cfg["fields"])
            raw_fields = raw.get("fields")
            if isinstance(raw_fields, dict):
                for k, v in raw_fields.items():
                    try:
                        fields[k] = 1 if int(v) == 1 else 0
                    except Exception:
                        fields[k] = 1 if bool(v) else 0
            cfg["fields"] = fields
        return cfg

    def _latency_bucket(self, ms: float) -> str:
        if ms < 250:
            return "FAST"
        if ms < 600:
            return "OK"
        if ms < 1200:
            return "SLOW"
        return "VERY SLOW"

    def _render_ui(
        self,
        *,
        enabled: bool,
        cfg: dict,
        env: str,
        rev: str,
        host: str,
        rid: str,
        status_code: int,
        is_error: bool,
        total_ms: float,
        db_time_ms: float,
        db_count: int,
        ext_total_ms: float,
        ext_total_n: int,
        vertex_ms: float,
        vertex_n: int,
        history: list,
    ) -> str:
        pos = cfg.get("pos", "br")
        pos_class = {"br": "dg-pos-br", "bl": "dg-pos-bl", "tr": "dg-pos-tr", "tl": "dg-pos-tl"}.get(pos, "dg-pos-br")
        compact_class = "dg-compact" if int(cfg.get("compact", 0)) == 1 else ""
        error_class = "dg-error" if is_error else ""

        fields = cfg.get("fields", {}) or {}

        def on(k: str) -> bool:
            return bool(int(fields.get(k, 0)))

        latency_bucket = self._latency_bucket(total_ms)
        status_badge = "OK" if status_code < 400 else ("WARN" if status_code < 500 else "FAIL")

        rows = []
        if on("env"):
            rows.append(("환경", env))
        if on("rev") and rev:
            rows.append(("리비전", rev))
        if on("host") and host:
            rows.append(("호스트", host))
        if on("status"):
            rows.append(("HTTP 상태", f"{status_code} ({status_badge})"))
        if on("latency"):
            rows.append(("지연", f"{total_ms:.1f} ms ({latency_bucket})"))
        if on("db"):
            rows.append(("DB", f"{db_time_ms:.1f} ms / {db_count} 쿼리"))
        if on("ext"):
            rows.append(("외부(HTTP)", f"{ext_total_ms:.1f} ms / {ext_total_n} 호출"))
        if on("vertex"):
            rows.append(("Vertex", f"{vertex_ms:.1f} ms / {vertex_n} 호출"))
        if on("rid") and rid:
            rows.append(("요청 ID", rid))

        rows_data = [{"k": k, "v": v, "copy": (k == "요청 ID")} for k, v in rows]

        boot = {
            "enabled": bool(enabled),
            "cfg": cfg,
            "pos": str(pos),
            "is_error": bool(is_error),
            "rows": rows_data,
            "history": history,
        }
        boot_json = json.dumps(boot, ensure_ascii=False).replace("<", "\\u003c")
        enabled_js = "true" if enabled else "false"

        panel_html = ""
        if enabled:
            panel_html = render_to_string("ragapp/obsbadge/panel.html")

        static_v = str(getattr(settings, "STATIC_VERSION", "") or "1")
        css_url = static("ragapp/obsbadge/admin_obs_badge.css") + f"?v={static_v}"
        js_url = static("ragapp/obsbadge/admin_obs_badge.js") + f"?v={static_v}"

        return f"""
<!-- Admin OBS (Injected) -->
<link rel="stylesheet" href="{css_url}">
<script type="application/json" id="dgObsBoot">{boot_json}</script>
<div id="dgObsRoot" class="{pos_class} {compact_class} {error_class}" data-enabled="{enabled_js}">
  <button type="button" id="dgObsLauncher">OBS</button>
  {panel_html}
</div>
<script defer src="{js_url}"></script>
<!-- /Admin OBS -->
"""
