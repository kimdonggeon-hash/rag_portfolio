# ragapp/obs/spans.py
from __future__ import annotations

import contextvars
import math
import time
from typing import Any, Dict, List

# -----------------------------------------------------------------------------
# Context (ASGI/async-safe)
# -----------------------------------------------------------------------------
_enabled_var: contextvars.ContextVar[bool] = contextvars.ContextVar("dg_obs_enabled", default=False)
_max_spans_var: contextvars.ContextVar[int] = contextvars.ContextVar("dg_obs_max_spans", default=80)
_spans_var: contextvars.ContextVar[List[Dict[str, Any]] | None] = contextvars.ContextVar("dg_obs_spans", default=None)

DEFAULT_MAX_SPANS = 80


def set_obs_enabled(enabled: bool) -> None:
    """요청 컨텍스트 단위 OBS 활성 플래그"""
    _enabled_var.set(bool(enabled))


def is_obs_enabled() -> bool:
    return bool(_enabled_var.get())


def set_max_spans(n: int) -> None:
    """요청 컨텍스트 단위 span 최대 보관 개수(링버퍼)"""
    try:
        n_i = int(n)
    except Exception:
        n_i = DEFAULT_MAX_SPANS
    _max_spans_var.set(max(10, min(500, n_i)))


def _buf() -> List[Dict[str, Any]]:
    b = _spans_var.get()
    if b is None:
        b = []
        _spans_var.set(b)
    return b


def clear_spans() -> None:
    _spans_var.set([])


def snapshot_spans() -> List[Dict[str, Any]]:
    """현재 컨텍스트에 쌓인 spans 복사본(유지)"""
    return list(_buf())


def drain_spans() -> List[Dict[str, Any]]:
    """현재 컨텍스트에 쌓인 spans 반환 후 비움"""
    out = list(_buf())
    _spans_var.set([])
    return out


def record(span_type: str, ms: float, **fields: Any) -> None:
    """
    Span 기록 (OBS 켜진 요청에서만 기록)
    - span_type: "http" 등
    - ms: duration ms
    - fields: group/lib/method/host/path/status/error 등
    """
    if not is_obs_enabled():
        return

    # ms 정규화(비정상 값 방어)
    try:
        ms_f = float(ms)
        if math.isnan(ms_f) or math.isinf(ms_f):
            ms_f = 0.0
    except Exception:
        ms_f = 0.0

    item: Dict[str, Any] = {
        "ts": time.time(),  # epoch seconds
        "type": str(span_type),
        "ms": round(ms_f, 3),
    }
    for k, v in fields.items():
        if not k or str(k).startswith("_"):
            continue
        item[str(k)] = v

    b = _buf()
    b.append(item)

    # ring buffer
    try:
        max_n = int(_max_spans_var.get() or DEFAULT_MAX_SPANS)
    except Exception:
        max_n = DEFAULT_MAX_SPANS

    if len(b) > max_n:
        del b[:-max_n]


# -----------------------------------------------------------------------------
# ✅ Compatibility layer for middleware imports
#   - ragapp.middleware.admin_obs_badge expects:
#       reset(), set_active(), snapshot() -> dict
# -----------------------------------------------------------------------------
def set_active(enabled: bool) -> None:
    # middleware가 기대하는 이름
    set_obs_enabled(enabled)


def reset() -> None:
    # middleware가 기대하는 이름
    clear_spans()


def snapshot() -> Dict[str, Any]:
    """
    middleware가 기대하는 집계 형태(dict) 반환.
    - total_ms/total_n: 외부 HTTP 전체 (vertex 포함)
    - vertex_ms/vertex_n: group == "vertex" subset
    """
    spans = snapshot_spans()

    total_ms = 0.0
    total_n = 0
    vertex_ms = 0.0
    vertex_n = 0

    for s in spans:
        if str(s.get("type", "")).lower() != "http":
            continue
        try:
            ms = float(s.get("ms") or 0.0)
        except Exception:
            ms = 0.0

        total_ms += ms
        total_n += 1

        if str(s.get("group", "")).lower() == "vertex":
            vertex_ms += ms
            vertex_n += 1

    return {
        "total_ms": round(total_ms, 3),
        "total_n": int(total_n),
        "vertex_ms": round(vertex_ms, 3),
        "vertex_n": int(vertex_n),
    }
