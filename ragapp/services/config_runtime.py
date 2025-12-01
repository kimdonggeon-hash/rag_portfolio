# ragapp/services/config_runtime.py
from django.conf import settings
from ragapp.models import RagSetting

_cache: dict[str, str | None] = {}

def _normalize_bool(v: str | None) -> bool:
    if v is None:
        return False
    s = str(v).strip().lower()
    if s in ("1","true","t","yes","y","on"):
        return True
    if s in ("0","false","f","no","n","off"):
        return False
    return bool(s)

def _normalize_int(v: str | None, default: int) -> int:
    try:
        return int(str(v).strip())
    except Exception:
        return default

def get_conf_raw(key: str, default: str | None = None) -> str | None:
    # 캐시
    if key in _cache:
        return _cache[key]
    # DB 우선
    try:
        row = RagSetting.objects.filter(key=key).first()
        if row and row.value is not None and row.value != "":
            _cache[key] = row.value
            return row.value
    except Exception:
        pass
    # settings / .env fallback
    fallback = getattr(settings, key, None)
    if fallback is None:
        fallback = default
    _cache[key] = fallback
    return fallback

def get_conf_bool(key: str, default_true: bool = True) -> bool:
    raw = get_conf_raw(key, default="1" if default_true else "0")
    return _normalize_bool(raw)

def get_conf_int(key: str, default_val: int) -> int:
    raw = get_conf_raw(key, default=str(default_val))
    return _normalize_int(raw, default_val)

def get_conf_str(key: str, default_val: str = "") -> str:
    raw = get_conf_raw(key, default_val)
    return "" if raw is None else str(raw)

def bust_cache(keys: list[str] | None = None):
    """관리자에서 값 바꾼 직후 강제 반영하고 싶을 때 호출(선택)."""
    global _cache
    if not keys:
        _cache = {}
    else:
        for k in keys:
            _cache.pop(k, None)
