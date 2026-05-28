from __future__ import annotations

import os
import time
import threading
import logging
from collections import deque

from django.http import HttpResponse, JsonResponse
from django.db import close_old_connections

log = logging.getLogger("ragapp.db_guard")


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except Exception:
        return default


class DBGuardMiddleware:
    """
    - 동시 DB 진입을 프로세스 단위로 제한(세마포어)
    - 에러가 연속되면 잠깐 쿨다운(서킷 오픈)
    """

    ALLOW_PREFIXES = (
        "/static/",
        "/legal/",
        "/favicon.ico",
        "/healthz",
        "/admin/",
        "/ragadmin/",
    )

    _lock = threading.Lock()
    _sem: threading.Semaphore | None = None

    _err_ts = deque()
    _circuit_open_until = 0.0

    def __init__(self, get_response):
        self.get_response = get_response

        self.enabled = _truthy(os.getenv("DB_GUARD_ENABLED", "1"))
        self.max_inflight = _env_int("DB_GUARD_MAX_INFLIGHT", 8)
        self.wait_ms = _env_int("DB_GUARD_WAIT_MS", 250)

        self.cooldown_s = _env_float("DB_GUARD_COOLDOWN_S", 3.0)
        self.err_window_s = _env_float("DB_GUARD_ERR_WINDOW_S", 20.0)
        self.open_on_errs = _env_int("DB_GUARD_OPEN_ON_ERRS", 6)

        with self._lock:
            if DBGuardMiddleware._sem is None:
                DBGuardMiddleware._sem = threading.Semaphore(max(1, self.max_inflight))

    def __call__(self, request):
        path = request.path or ""
        if (not self.enabled) or path.startswith(self.ALLOW_PREFIXES):
            return self.get_response(request)

        now = time.monotonic()

        # 서킷 오픈 상태면 즉시 폴백
        if now < self._circuit_open_until:
            return self._busy_response(request, reason="cooldown")

        sem = DBGuardMiddleware._sem
        assert sem is not None

        acquired = sem.acquire(timeout=max(0.0, self.wait_ms / 1000.0))
        if not acquired:
            return self._busy_response(request, reason="throttled")

        try:
            return self.get_response(request)
        except Exception:
            self._record_error_and_maybe_open_circuit()
            raise
        finally:
            sem.release()
            close_old_connections()

    def _record_error_and_maybe_open_circuit(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._err_ts.append(now)
            cutoff = now - self.err_window_s
            while self._err_ts and self._err_ts[0] < cutoff:
                self._err_ts.popleft()

            if len(self._err_ts) >= self.open_on_errs:
                self._circuit_open_until = now + self.cooldown_s
                log.warning(
                    "DBGuard circuit OPEN (errs=%s window=%.1fs cooldown=%.1fs)",
                    len(self._err_ts), self.err_window_s, self.cooldown_s
                )

    def _busy_response(self, request, reason: str):
        accept = (request.headers.get("Accept") or "").lower()
        payload = {"ok": False, "code": "DB_BUSY", "reason": reason}

        if "application/json" in accept or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse(payload, status=503)

        return HttpResponse(
            "서버가 잠시 바쁩니다(DB 보호 모드). 잠시 후 다시 시도해 주세요.",
            status=503,
            content_type="text/plain; charset=utf-8",
        )
