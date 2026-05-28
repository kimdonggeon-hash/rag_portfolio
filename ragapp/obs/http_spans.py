# ragapp/obs/http_spans.py
from __future__ import annotations

import os
import time
from urllib.parse import urlparse
from typing import Any

from .spans import record, is_obs_enabled

_INSTALLED = False


def _truthy(v: str | None) -> bool:
    s = (v or "").strip().lower()
    return s in ("1", "true", "t", "yes", "y", "on")


def _host_of(url: Any) -> str:
    try:
        u = str(url)
        p = urlparse(u)
        return (p.netloc or "").lower()
    except Exception:
        return ""


def _path_of(url: Any) -> str:
    try:
        u = str(url)
        p = urlparse(u)
        return (p.path or "")[:200]
    except Exception:
        return ""


def _is_vertex_host(host: str) -> bool:
    h = (host or "").lower()
    if "aiplatform.googleapis.com" in h:
        return True
    if h.endswith("-aiplatform.googleapis.com"):
        return True
    if "generativelanguage.googleapis.com" in h:
        return True
    return False


def install_http_spans() -> None:
    """
    requests/httpx 외부 호출 시간을 OBS span으로 기록.

    안전 조건:
    - DG_OBS_DISABLE_HTTP_SPANS=1이면 아예 설치 안 함
    - 여러 번 호출되어도 1회만 설치
    - 설치/기록 과정에서 예외가 나도 서비스는 절대 죽지 않음
    """
    global _INSTALLED
    if _INSTALLED:
        return

    if _truthy(os.environ.get("DG_OBS_DISABLE_HTTP_SPANS")):
        _INSTALLED = True
        return

    try:
        _patch_requests()
    except Exception:
        # 절대 크래시 금지
        pass

    try:
        _patch_httpx()
    except Exception:
        # 절대 크래시 금지
        pass

    _INSTALLED = True


def _patch_requests() -> None:
    try:
        import requests  # type: ignore
        from requests.sessions import Session  # type: ignore
    except Exception:
        return

    orig = Session.request
    if getattr(orig, "__dg_obs_wrapped__", False):
        return

    def _wrapped(self, method, url, *args, __orig=orig, **kwargs):  # type: ignore
        # OBS 꺼졌으면 오버헤드 최소화
        if not is_obs_enabled():
            return __orig(self, method, url, *args, **kwargs)

        t0 = time.perf_counter()
        host = _host_of(url)
        path = _path_of(url)
        group = "vertex" if _is_vertex_host(host) else "external"
        status = 0
        err: str | None = None

        try:
            resp = __orig(self, method, url, *args, **kwargs)
            status = int(getattr(resp, "status_code", 0) or 0)
            return resp
        except Exception as e:
            err = repr(e)
            raise
        finally:
            ms = (time.perf_counter() - t0) * 1000.0
            record(
                "http",
                ms,
                group=group,
                lib="requests",
                method=str(method or "").upper(),
                host=host,
                path=path,
                status=status,
                error=err,
            )

    _wrapped.__dg_obs_wrapped__ = True  # type: ignore
    Session.request = _wrapped  # type: ignore


def _patch_httpx() -> None:
    try:
        import httpx  # type: ignore
    except Exception:
        return

    # ---- sync client ----
    if hasattr(httpx, "Client"):
        orig = httpx.Client.request
        if not getattr(orig, "__dg_obs_wrapped__", False):

            def _wrapped(self, method, url, *args, __orig=orig, **kwargs):  # type: ignore
                if not is_obs_enabled():
                    return __orig(self, method, url, *args, **kwargs)

                t0 = time.perf_counter()
                host = _host_of(url)
                path = _path_of(url)
                group = "vertex" if _is_vertex_host(host) else "external"
                status = 0
                err: str | None = None

                try:
                    resp = __orig(self, method, url, *args, **kwargs)
                    status = int(getattr(resp, "status_code", 0) or 0)
                    return resp
                except Exception as e:
                    err = repr(e)
                    raise
                finally:
                    ms = (time.perf_counter() - t0) * 1000.0
                    record(
                        "http",
                        ms,
                        group=group,
                        lib="httpx",
                        method=str(method or "").upper(),
                        host=host,
                        path=path,
                        status=status,
                        error=err,
                    )

            _wrapped.__dg_obs_wrapped__ = True  # type: ignore
            httpx.Client.request = _wrapped  # type: ignore

    # ---- async client ----
    if hasattr(httpx, "AsyncClient"):
        aorig = httpx.AsyncClient.request
        if not getattr(aorig, "__dg_obs_wrapped__", False):

            async def _wrapped_async(self, method, url, *args, __orig=aorig, **kwargs):  # type: ignore
                if not is_obs_enabled():
                    return await __orig(self, method, url, *args, **kwargs)

                t0 = time.perf_counter()
                host = _host_of(url)
                path = _path_of(url)
                group = "vertex" if _is_vertex_host(host) else "external"
                status = 0
                err: str | None = None

                try:
                    resp = await __orig(self, method, url, *args, **kwargs)
                    status = int(getattr(resp, "status_code", 0) or 0)
                    return resp
                except Exception as e:
                    err = repr(e)
                    raise
                finally:
                    ms = (time.perf_counter() - t0) * 1000.0
                    record(
                        "http",
                        ms,
                        group=group,
                        lib="httpx-async",
                        method=str(method or "").upper(),
                        host=host,
                        path=path,
                        status=status,
                        error=err,
                    )

            _wrapped_async.__dg_obs_wrapped__ = True  # type: ignore
            httpx.AsyncClient.request = _wrapped_async  # type: ignore
