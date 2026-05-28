# ragapp/services/runtime_metrics.py
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from django.conf import settings
from django.utils import timezone
from django.db.models import Q

try:
    from ragapp.models import ChatQueryLog, LiveChatSession  # type: ignore
except Exception:  # pragma: no cover
    ChatQueryLog = None  # type: ignore
    LiveChatSession = None  # type: ignore


def _int_setting(name: str, default: int) -> int:
    try:
        v = getattr(settings, name, default)
        return int(v)
    except Exception:
        return default


def _bool_setting(name: str, default: bool = False) -> bool:
    try:
        v = getattr(settings, name, default)
        s = str(v).strip().lower()
        return s not in ("0", "false", "no", "off", "", "none", "null")
    except Exception:
        return default


def _media_snapshot() -> Dict[str, Any]:
    """
    MEDIA_ROOT 기준 업로드 폴더 상태 스냅샷.
    - 파일 개수 / 총 용량(MB)까지 계산 (포트폴리오 규모라 완전 os.walk 해도 무방)
    """
    root = getattr(settings, "MEDIA_ROOT", None)
    if not root:
        return {
            "media_root": "",
            "media_exists": False,
            "media_is_tmp": False,
            "file_count": 0,
            "total_mb": 0.0,
            "scan_limited": False,
            "retention_days": _int_setting("RETENTION_DAYS", 30),
            "retention_chatlog_days": _int_setting("RETENTION_DAYS_CHATLOG", 90),
            "retention_livechat_days": _int_setting("RETENTION_DAYS_LIVECHAT", 180),
            "auto_purge_enabled": _bool_setting("AUTO_PURGE_ENABLED", False),
        }

    path = Path(root)
    exists = path.exists()
    is_tmp = str(path).startswith("/tmp")

    file_count = 0
    total_bytes = 0
    scan_limited = False

    if exists and path.is_dir():
        try:
            max_files = 5000  # 혹시라도 너무 많아지면 여기까지만 스캔
            for dirpath, dirnames, filenames in os.walk(path):
                for fname in filenames:
                    file_count += 1
                    if file_count > max_files:
                        scan_limited = True
                        break
                    fp = Path(dirpath) / fname
                    try:
                        total_bytes += fp.stat().st_size
                    except Exception:
                        pass
                if scan_limited:
                    break
        except Exception:
            # 스캔 중 에러나도 서비스에 영향 없게
            pass

    total_mb = round(total_bytes / (1024.0 * 1024.0), 1) if total_bytes else 0.0

    return {
        "media_root": str(path),
        "media_exists": exists,
        "media_is_tmp": is_tmp,
        "file_count": file_count,
        "total_mb": total_mb,
        "scan_limited": scan_limited,
        "retention_days": _int_setting("RETENTION_DAYS", 30),
        "retention_chatlog_days": _int_setting("RETENTION_DAYS_CHATLOG", 90),
        "retention_livechat_days": _int_setting("RETENTION_DAYS_LIVECHAT", 180),
        "auto_purge_enabled": _bool_setting("AUTO_PURGE_ENABLED", False),
    }


def _chat_stats(window_sec: int) -> Dict[str, Any]:
    """
    ChatQueryLog 기준:
    - 최근 window_sec 초 동안의 요청/유저 수
    - 오늘 전체 요청 수 / 429 수
    """
    now = timezone.now()
    today = timezone.localdate()
    window_start = now - timezone.timedelta(seconds=window_sec)

    stats = {
        "recent_users": 0,
        "recent_requests": 0,
        "today_total": 0,
        "today_429": 0,
    }

    if ChatQueryLog is None:
        return stats

    try:
        qs = ChatQueryLog.objects.all()

        # 최근 구간
        recent_qs = qs.filter(created_at__gte=window_start)
        stats["recent_requests"] = recent_qs.count()

        # 사용자 수: client_key / ip_hash / session_id 중 존재하는 필드로 근사
        field_names = {f.name for f in ChatQueryLog._meta.get_fields() if hasattr(f, "attname")}
        if "client_key" in field_names:
            stats["recent_users"] = (
                recent_qs.exclude(client_key="")
                .values("client_key")
                .distinct()
                .count()
            )
        elif "ip_hash" in field_names:
            stats["recent_users"] = (
                recent_qs.exclude(ip_hash="")
                .values("ip_hash")
                .distinct()
                .count()
            )
        else:
            stats["recent_users"] = (
                recent_qs.exclude(session_id="")
                .values("session_id")
                .distinct()
                .count()
            )

        # 오늘 요청
        today_qs = qs.filter(created_at__date=today)
        stats["today_total"] = today_qs.count()
        if "http_status" in field_names:
            stats["today_429"] = today_qs.filter(http_status=429).count()
    except Exception:
        # 통계 실패해도 대시보드가 죽지는 않게
        pass

    return stats


def _livechat_open_sessions() -> int:
    """
    LiveChatSession 기준 '열려 있는' 세션 수 대략 집계.
    - status / is_active / ended_at 필드가 있으면 활용
    """
    if LiveChatSession is None:
        return 0

    try:
        qs = LiveChatSession.objects.all()
        field_names = {f.name for f in LiveChatSession._meta.get_fields() if hasattr(f, "attname")}

        cond = Q()
        if "status" in field_names:
            cond &= ~Q(status__in=["ended", "종료", "closed", "done", "완료"])
        if "is_active" in field_names:
            cond &= Q(is_active=True)
        if "ended_at" in field_names:
            cond &= Q(ended_at__isnull=True)

        if cond:
            qs = qs.filter(cond)
        return qs.count()
    except Exception:
        return 0


def _status_label(recent_requests: int) -> str:
    """
    간단한 상태 라벨:
    - 0 ~ 4  : idle
    - 5 ~ 19 : normal
    - 20+    : busy
    """
    if recent_requests >= 20:
        return "busy"
    if recent_requests >= 5:
        return "normal"
    return "idle"


def get_runtime_snapshot(window_sec: int = 300) -> Dict[str, Any]:
    """
    /ragadmin/runtime/ 에서 쓰는 통합 스냅샷.
    - window_sec: 최근 N초 기준으로 동시 접속/요청 근사.
    """
    now = timezone.now()
    today = timezone.localdate()

    chat_stats = _chat_stats(window_sec)
    livechat_open = _livechat_open_sessions()
    media = _media_snapshot()

    status = _status_label(chat_stats["recent_requests"])

    snapshot: Dict[str, Any] = {
        "now": now,
        "today": today,
        "window_sec": window_sec,
        "status": status,
        "recent_users": chat_stats["recent_users"],
        "recent_requests": chat_stats["recent_requests"],
        "today_total": chat_stats["today_total"],
        "today_429": chat_stats["today_429"],
        "livechat_open": livechat_open,
        # 업로드/보관 관련
        **media,
    }

    # alias (혹시 다른 곳에서 active_sessions를 쓰고 있으면 위해)
    snapshot.setdefault("active_sessions", snapshot["recent_users"])
    snapshot.setdefault("soft_deleted_uploads", 0)

    return snapshot
