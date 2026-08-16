# ragapp/livechat/consumers.py
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

# ✅ PII guard (저장/전송 직전 차단) - import 실패해도 WS 부팅/동작은 살아야 함
try:
    from ragapp.pii import guard_text as _guard_text_impl, summarize_hits as _summarize_hits_impl  # type: ignore
except Exception:  # pragma: no cover
    _guard_text_impl = None  # type: ignore
    _summarize_hits_impl = None  # type: ignore

# ✅ PII fallback detector (HTTP 미들웨어와 동일한 엔진 사용)
try:
    from ragapp.utils.pii_guard import detect_pii_any as _detect_pii_any_impl  # type: ignore
except Exception:  # pragma: no cover
    _detect_pii_any_impl = None  # type: ignore

from ragapp.livechat import agent_api

# ✅ 단일 저장/조회 유틸 (핵심)
from ragapp.livechat.store import save_ws_message, ensure_room_row

log = logging.getLogger(__name__)

try:
    from ragapp.models import LiveChatSession  # type: ignore
except Exception:  # pragma: no cover
    LiveChatSession = None  # type: ignore

_END_TYPES = {"end", "closed", "close"}
_STANDARD_END_MESSAGE = (
    "상담을 종료했습니다. 추가로 궁금한 점이 생기면 언제든지 "
    "질문 챗봇이나 실시간 상담을 이용해 주세요."
)

_CLOSED_STATUS_FALLBACKS = {
    "ended",
    "closed",
    "close",
    "done",
    "saved",
    "종료",
    "ended_need_save",
}


def _status_to_str(v: Any) -> str:
    try:
        return str(getattr(v, "value", v) or "").strip().lower()
    except Exception:
        return ""


def _closed_statuses() -> set[str]:
    values = set(_CLOSED_STATUS_FALLBACKS)

    try:
        if LiveChatSession is not None:
            Status = getattr(LiveChatSession, "Status", None)
            if Status is not None:
                for name in ("ENDED", "CLOSED", "DONE", "SAVED", "ENDED_NEED_SAVE"):
                    if hasattr(Status, name):
                        values.add(_status_to_str(getattr(Status, name)))
    except Exception:
        pass

    return values


def _is_session_closed_obj(session: Any) -> bool:
    """
    종료된 상담인지 판단.
    - status가 종료 계열이면 종료
    - ended_at이 있으면 종료
    """
    if session is None:
        return True

    try:
        if getattr(session, "ended_at", None):
            return True

        status = _status_to_str(getattr(session, "status", ""))
        if status in _closed_statuses():
            return True
    except Exception:
        return False

    return False

# ─────────────────────────────────────
# settings safe getter (import 시점/초기화 타이밍 이슈 방지)
# ─────────────────────────────────────
def _get_setting(name: str, default: Any) -> Any:
    try:
        return getattr(settings, name, default)
    except ImproperlyConfigured:
        return default
    except Exception:
        return default


# ─────────────────────────────────────
# 욕설/모욕/성희롱 차단(전송 직전)
# - settings에서 LIVECHAT_ABUSE_PATTERNS / LIVECHAT_SEXUAL_PATTERNS 로 덮어쓰기 가능
# ※ 중요: import 시점에 settings 접근 금지 → lazy compile로 변경
# ─────────────────────────────────────
_DEFAULT_ABUSE_PATTERNS = [
    r"(씨\s*발|ㅅ\s*ㅂ)",
    r"(병\s*신)",
    r"(개\s*새\s*끼)",
    r"(좆)",
    r"(멍청(하|해))",
]
_DEFAULT_SEXUAL_PATTERNS = [
    r"(성희롱)",
    r"(섹스)",
    r"(야동)",
]


def _compile_patterns(patterns) -> list[re.Pattern]:
    out: list[re.Pattern] = []
    for p in (patterns or []):
        try:
            out.append(re.compile(str(p), re.IGNORECASE))
        except Exception:
            continue
    return out


# ✅ lazy cache
_LC_ABUSE_RE: Optional[list[re.Pattern]] = None
_LC_SEXUAL_RE: Optional[list[re.Pattern]] = None


def _get_abuse_regexes() -> list[re.Pattern]:
    global _LC_ABUSE_RE
    if _LC_ABUSE_RE is None:
        pats = _get_setting("LIVECHAT_ABUSE_PATTERNS", _DEFAULT_ABUSE_PATTERNS)
        _LC_ABUSE_RE = _compile_patterns(pats)
    return _LC_ABUSE_RE


def _get_sexual_regexes() -> list[re.Pattern]:
    global _LC_SEXUAL_RE
    if _LC_SEXUAL_RE is None:
        pats = _get_setting("LIVECHAT_SEXUAL_PATTERNS", _DEFAULT_SEXUAL_PATTERNS)
        _LC_SEXUAL_RE = _compile_patterns(pats)
    return _LC_SEXUAL_RE


def _match_any(regexes: list[re.Pattern], text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    for rx in regexes:
        try:
            if rx.search(t):
                return True
        except Exception:
            continue
    return False


def _boolish(v: Any, default: bool = True) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "t", "yes", "y", "on"):
        return True
    if s in ("0", "false", "f", "no", "n", "off"):
        return False
    return default


def _end_on(kind: str) -> bool:
    """
    기본값 True: 직원 보호 목적이면 차단 후 즉시 종료가 안전.
    settings로 끌 수 있음:
      LIVECHAT_END_ON_ABUSE=False
      LIVECHAT_END_ON_SEXUAL=False
      LIVECHAT_END_ON_PII=False
    """
    if kind == "abuse":
        return _boolish(_get_setting("LIVECHAT_END_ON_ABUSE", True), default=True)
    if kind == "sexual":
        return _boolish(_get_setting("LIVECHAT_END_ON_SEXUAL", True), default=True)
    if kind == "pii":
        return _boolish(_get_setting("LIVECHAT_END_ON_PII", True), default=True)
    return True


# ─────────────────────────────────────
# PII helper (guard_text 미존재/오류에도 안전)
# - ragapp.pii가 있으면 우선 사용
# - 그 다음 detect_pii_any fallback으로 "카드/계좌 포함" 전역 차단 유지
# ─────────────────────────────────────
def _guard_text(text: str) -> Tuple[bool, Any]:
    """
    returns: (ok, hits)
      ok=True  -> 통과
      ok=False -> PII 감지
    """
    t = str(text or "")

    # 1) 기존 ragapp.pii 사용(있으면)
    try:
        if callable(_guard_text_impl):
            ok, hits = _guard_text_impl(t)
            if not ok:
                return False, hits
            # ✅ 기존 guard가 통과해도, fallback까지 한 번 더 검사해서 정책 일치(카드/계좌 등)
    except Exception:
        # 가드 자체가 터지면 "안전" 쪽으로 = 차단
        return False, [{"type": "pii", "value": "guard_error"}]

    # 2) fallback detector (HTTP 미들웨어와 동일 엔진)
    try:
        if callable(_detect_pii_any_impl):
            r = _detect_pii_any_impl(t)
            if getattr(r, "hit", False):
                return False, [{"type": "pii", "kind": getattr(r, "kind", "PII")}]
            return True, []
    except Exception:
        return False, [{"type": "pii", "value": "fallback_error"}]

    # 3) 둘 다 없으면(비정상) → 전부 차단 정책이면 막는 게 안전
    return False, [{"type": "pii", "value": "no_detector"}]


def _summarize_hits(hits: Any) -> str:
    try:
        if callable(_summarize_hits_impl):
            return _summarize_hits_impl(hits) or ""
    except Exception:
        return ""
    return ""


# ─────────────────────────────────────
# 안전 로그 (본문/PII 금지)
# ─────────────────────────────────────
def _safe_log(event: str, **meta: Any) -> None:
    try:
        payload = {"event": event, **meta}
        log.info("[LIVECHAT] %s", json.dumps(payload, ensure_ascii=False, default=str)[:4000])
    except Exception:
        log.info("[LIVECHAT] %s meta=%s", event, str(meta)[:1000])


# ─────────────────────────────────────
# 기본 텍스트
# ─────────────────────────────────────
AUTO_GREETING_TEXT = (
    "상담사가 연결되었습니다. 안녕하세요, 김동건 포트폴리오 실시간 상담입니다. 무엇을 도와드릴까요?"
)


def _safe_group_name(s: str) -> str:
    out = []
    for ch in (s or ""):
        if ch.isalnum() or ch in ("_", "-", "."):
            out.append(ch)
        else:
            out.append("_")
    g = "".join(out)[:80]
    return g or "unknown"


def _to_int(v: Any) -> Optional[int]:
    try:
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        s = str(v).strip()
        if not s:
            return None
        return int(s)
    except Exception:
        return None


def _ts_ms(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        if isinstance(v, int):
            return v * 1000 if v < 10_000_000_000 else v
        if isinstance(v, float):
            i = int(v)
            return i * 1000 if i < 10_000_000_000 else i
        s = str(v).strip()
        if not s:
            return None
        if s.isdigit():
            i = int(s)
            return i * 1000 if i < 10_000_000_000 else i
    except Exception:
        pass
    return None


def _normalize_sender_role(sender: str, max_len: int = 16) -> Tuple[str, bool]:
    raw = (sender or "").strip()
    s = raw.lower()

    if "operator" in s or s in ("master", "admin", "staff"):
        norm = "operator"
    elif "user" in s or s in ("client", "customer", "visitor"):
        norm = "user"
    elif s in ("system", "bot", "assistant"):
        norm = "system"
    elif not s:
        norm = "system"
    else:
        norm = raw

    trimmed = False
    if len(norm) > max_len:
        norm = norm[:max_len]
        trimmed = True
    return norm, trimmed


def _unwrap_payload(d: Any) -> Dict[str, Any]:
    """
    프론트/브로드캐스트 형태가 다양해서 payload 래핑을 흡수.
    - {"payload": {...}} 형태면 payload를 우선 사용
    """
    if isinstance(d, dict):
        p = d.get("payload")
        if isinstance(p, dict):
            merged = dict(d)
            merged.update(p)
            return merged
        return d
    return {}


def _pick_first(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d and d.get(k) is not None:
            v = d.get(k)
            if isinstance(v, str):
                if v.strip() != "":
                    return v
            else:
                return v
    return default


def _pick_sender_raw(d: Dict[str, Any]) -> str:
    return str(
        _pick_first(
            d,
            "role",
            "sender",
            "from",
            "author",
            "user_role",
            default="system",
        )
        or "system"
    )


def _pick_text(d: Dict[str, Any]) -> str:
    v = _pick_first(d, "content", "text", "message", "body", "msg", default="")
    return str(v or "")


def _pick_type(d: Dict[str, Any]) -> str:
    t = _pick_first(d, "type", "msg_type", "event", default="message")
    return str(t or "message").lower()


def _pick_room(d: Dict[str, Any], fallback_room: str) -> str:
    r = _pick_first(d, "room", "room_id", "roomId", "rid", default=fallback_room)
    return str(r or fallback_room or "unknown")


@database_sync_to_async
def _ensure_room_row_async(room: str) -> None:
    ensure_room_row(room=room)


# ─────────────────────────────────────
# WS 동시 접속 제한 (Cloud Run max instances=1이면 이걸로 충분)
# settings로 덮어쓰기 가능:
#   LIVECHAT_MAX_WS_CONNECTIONS=10
#   LIVECHAT_MAX_WS_CONNECTIONS_PER_ROOM=3
#   LIVECHAT_WS_SLOT_TTL=3700
# ─────────────────────────────────────
def _lc_limit_enabled() -> bool:
    """
    포트폴리오 서비스에서는 기본적으로 WS 접속 수 카운터 제한을 끈다.
    서버 보호는 Cloud Run 설정 + 메시지 rate limit + 상담 세션 제한으로 처리.
    """
    return _boolish(_get_setting("LIVECHAT_WS_LIMIT_ENABLED", False), default=False)


def _lc_ttl() -> int:
    v = _to_int(_get_setting("LIVECHAT_WS_SLOT_TTL", 300))
    return int(v or 300)


def _lc_max_total() -> int:
    v = _to_int(_get_setting("LIVECHAT_MAX_WS_CONNECTIONS", 100))
    return int(v or 100)


def _lc_max_per_room() -> int:
    v = _to_int(_get_setting("LIVECHAT_MAX_WS_CONNECTIONS_PER_ROOM", 20))
    return int(v or 20)

def _cache_get_int(key: str) -> int:
    try:
        v = cache.get(key)
        return int(v) if v is not None else 0
    except Exception:
        return 0


def _cache_set_int(key: str, value: int, ttl: int) -> None:
    try:
        cache.set(key, int(value), ttl)
    except Exception:
        pass


def _cache_incr(key: str, ttl: int, delta: int = 1) -> int:
    try:
        cache.add(key, 0, ttl)
        return int(cache.incr(key, delta))  # type: ignore
    except Exception:
        cur = _cache_get_int(key)
        nxt = max(0, cur + int(delta))
        _cache_set_int(key, nxt, ttl)
        return nxt


def _cache_decr(key: str, ttl: int, delta: int = 1) -> int:
    try:
        cache.add(key, 0, ttl)
        return int(cache.decr(key, delta))  # type: ignore
    except Exception:
        cur = _cache_get_int(key)
        nxt = max(0, cur - int(delta))
        _cache_set_int(key, nxt, ttl)
        return nxt


def _lc_key_total() -> str:
    return "livechat:ws:total"


def _lc_key_room(room: str) -> str:
    return f"livechat:ws:room:{_safe_group_name(room)}"

def _lc_user_msg_limit_per_min() -> int:
    """
    고객 메시지 분당 제한.
    0 이하이면 제한 비활성화.
    """
    v = _to_int(_get_setting("LIVECHAT_USER_MSG_LIMIT_PER_MIN", 20))
    return int(v or 20)


def _lc_user_rate_key(room: str) -> str:
    return f"livechat:rate:user:{_safe_group_name(room)}"


def _check_user_message_rate(room: str) -> Tuple[bool, int, int]:
    """
    returns: (ok, current_count, limit)
    """
    limit = _lc_user_msg_limit_per_min()
    if limit <= 0:
        return True, 0, limit

    key = _lc_user_rate_key(room)
    count = _cache_incr(key, 60, 1)

    return count <= limit, count, limit


def _acquire_ws_slot(room: str) -> Tuple[bool, str]:
    """
    returns (ok, reason)
      reason: "busy_total" | "busy_room" | "ok"

    기본값은 제한 OFF.
    제한을 끄면 가짜 혼잡 상태가 발생하지 않는다.
    """
    if not _lc_limit_enabled():
        return True, "ok"

    ttl = _lc_ttl()

    total = _cache_incr(_lc_key_total(), ttl, 1)
    if total > _lc_max_total():
        _cache_decr(_lc_key_total(), ttl, 1)
        return False, "busy_total"

    rk = _lc_key_room(room)
    per = _cache_incr(rk, ttl, 1)
    if per > _lc_max_per_room():
        _cache_decr(rk, ttl, 1)
        _cache_decr(_lc_key_total(), ttl, 1)
        return False, "busy_room"

    return True, "ok"


def _release_ws_slot(room: str) -> None:
    if not _lc_limit_enabled():
        return

    ttl = _lc_ttl()
    _cache_decr(_lc_key_room(room), ttl, 1)
    _cache_decr(_lc_key_total(), ttl, 1)


# ─────────────────────────────────────
# MasterConsumer
# ─────────────────────────────────────
class MasterConsumer(AsyncWebsocketConsumer):
    """
    운영자(마스터) 알림 채널.
    ✅ 운영자 메시지가 master WS로 들어와도
       - room 지정이 있으면 해당 room으로 브로드캐스트 + DB 저장까지 처리
    """

    group_name = "livechat_master"

    async def connect(self):
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        try:
            await self.send(text_data=json.dumps({"type": "master_connected"}))
        except Exception:
            pass

        try:
            agent_api.mark_operator_online()
        except Exception:
            log.warning("mark_operator_online failed", exc_info=True)

    async def disconnect(self, close_code):
        try:
            agent_api.mark_operator_offline()
        except Exception:
            log.warning("mark_operator_offline failed", exc_info=True)

        try:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        except Exception:
            pass

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return

        try:
            raw = json.loads(text_data)
        except Exception:
            raw = {}

        data = _unwrap_payload(raw)
        room = _pick_room(data, fallback_room="")
        if not room:
            return

        sender_raw = _pick_sender_raw(data)
        text = _pick_text(data)
        msg_type = _pick_type(data)

        session_id = _to_int(_pick_first(data, "session_id", "sessionId", "sid", default=None))
        ts = _ts_ms(_pick_first(data, "ts", default=None)) or int(timezone.now().timestamp() * 1000)

        sender_norm, _ = _normalize_sender_role(sender_raw, max_len=16)
        if sender_norm not in ("operator", "system", "user"):
            sender_norm = "operator"

        effective_type = msg_type or "message"

        # ✅ Master에서도 PII 차단 (전역 정책)
        if effective_type not in _END_TYPES:
            ok, hits = _guard_text(str(text))
            if not ok:
                msg = (
                    _summarize_hits(hits)
                    or _get_setting("PII_BLOCK_MESSAGE", "개인정보(카드/계좌 등)는 입력할 수 없습니다.")
                )
                try:
                    await self.send(
                        text_data=json.dumps(
                            {
                                "type": "blocked",
                                "code": "PII_BLOCKED",
                                "sender": "system",
                                "role": "system",
                                "text": msg,
                                "message": msg,
                                "room": room,
                                "session_id": session_id,
                                "ts": ts,
                            },
                            ensure_ascii=False,
                        )
                    )
                except Exception:
                    pass

                _safe_log("master_block", reason="pii", room=room, sid=session_id, len=len(text))

                if _end_on("pii"):
                    try:
                        await self.close(code=1000)
                    except Exception:
                        pass
                return

        _safe_log(
            "master_recv",
            room=room,
            sid=session_id,
            sender=sender_norm,
            type=effective_type,
            len=len(text),
        )

        payload = {
            "type": effective_type,
            "sender": sender_raw,  # 화면 표시용 원문
            "role": sender_norm,  # ✅ 표준 role
            "text": text,
            "content": text,  # ✅ 프론트 호환
            "ts": ts,
            "room": room,
            "session_id": session_id,
        }

        group_name = "livechat_room_" + _safe_group_name(room)

        try:
            await self.channel_layer.group_send(group_name, {"type": "room_message", "payload": payload})
        except Exception as e:
            _safe_log("master_warn", reason="group_send_failed", room=room, sid=session_id, err=str(e)[:200])

        # ✅ DB 저장 (operator 저장 마스킹은 store.py에서 처리됨)
        try:
            persist = _boolish(_get_setting("LIVECHAT_PERSIST_MESSAGES", True), default=True)
            if persist:
                msg_id = await database_sync_to_async(save_ws_message)(
                    room=room,
                    session_id=session_id,
                    sender_norm=sender_norm,
                    effective_type=effective_type,
                    body=str(text),
                    ts=ts,
                )
                _safe_log(
                    "save_ok" if msg_id else "save_skip",
                    room=room,
                    sid=session_id,
                    sender=sender_norm,
                    type=effective_type,
                    msg_id=msg_id,
                )
        except Exception as e:
            _safe_log("save_fail", room=room, sid=session_id, err_cls=e.__class__.__name__, err=str(e)[:200])

    async def broadcast(self, event: Dict[str, Any]):
        payload = event.get("payload") or {}
        try:
            await self.send(text_data=json.dumps(payload, default=str))
        except Exception as e:
            log.warning("MasterConsumer send failed: %s", e)


# ─────────────────────────────────────
# RoomConsumer
# ─────────────────────────────────────
class RoomConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room = self.scope["url_route"]["kwargs"].get("room") or "unknown"
        self.group_name = "livechat_room_" + _safe_group_name(str(self.room))

        # accept는 안내 메시지 보내려면 필요
        await self.accept()

        # ✅ WS 접속 수 카운터 제한은 사용하지 않음
        # 서버 보호는 Cloud Run 설정 + 상담 세션 제한 + 메시지 rate limit으로 처리
        self._slot_ok = True

        # ✅ 그룹 참가
        try:
            await self.channel_layer.group_add(self.group_name, self.channel_name)
        except Exception:
            pass

        try:
            await _ensure_room_row_async(room=str(self.room))
        except Exception:
            pass

        # A staff account may also open the public customer widget in the same
        # browser. Only the operator console's explicit connection may accept a
        # waiting request and move it to active.
        query_string = (self.scope.get("query_string") or b"").decode("utf-8", "ignore")
        is_operator_connection = "operator=1" in query_string.split("&")
        if is_operator_connection:
            active_payload = await self._mark_active_for_staff()
            if active_payload:
                await self._broadcast_master(active_payload)

        try:
            await self.send(text_data=json.dumps({"type": "room_connected", "room": self.room}))
        except Exception:
            pass

        _safe_log("ws_connected", room=str(self.room), group=self.group_name)

    async def disconnect(self, close_code):
        try:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        except Exception:
            pass

        _safe_log("ws_disconnected", room=str(getattr(self, "room", "unknown")), code=close_code)

    async def _broadcast_master(self, payload: Dict[str, Any]):
        try:
            await self.channel_layer.group_send("livechat_master", {"type": "broadcast", "payload": payload})
        except Exception as e:
            log.warning("broadcast master failed: %s", e)

    @database_sync_to_async
    def _mark_active_for_staff(self) -> Optional[Dict[str, Any]]:
        user = self.scope.get("user")
        if not (
            getattr(user, "is_authenticated", False)
            and getattr(user, "is_staff", False)
            and LiveChatSession is not None
        ):
            return None

        try:
            session = LiveChatSession.objects.filter(room=self.room).order_by("-id").first()
            if session is None or _is_session_closed_obj(session):
                return None

            # 이미 다른 상담사가 담당 중인 active 상담은 목록을 열람만 해도
            # 담당자가 조용히 바뀌지 않도록 가로채기(hijack)를 막는다.
            already_owned_by_other = (
                getattr(session, "status", None) == "active"
                and hasattr(session, "assigned_staff_id")
                and session.assigned_staff_id
                and session.assigned_staff_id != user.id
            )
            if already_owned_by_other:
                return None

            now = timezone.now()
            update_fields: List[str] = []
            if getattr(session, "status", None) != "active":
                session.status = "active"
                update_fields.append("status")
            if not getattr(session, "started_at", None):
                session.started_at = now
                update_fields.append("started_at")
            if hasattr(session, "assigned_staff_id") and session.assigned_staff_id != user.id:
                session.assigned_staff = user
                update_fields.append("assigned_staff")
            if update_fields:
                session.save(update_fields=update_fields)

            return {
                "type": "session_started",
                "room": session.room,
                "session_id": session.id,
                "status": session.status,
                "ts": int(now.timestamp() * 1000),
            }
        except Exception:
            log.warning("mark livechat active failed", exc_info=True)
            return None

    @database_sync_to_async
    def _resolve_session_id_by_room(self, room: str) -> Optional[int]:
        if LiveChatSession is None:
            return None
        try:
            s = LiveChatSession.objects.filter(room=room).order_by("-id").first()
            if s:
                return int(s.id)
        except Exception:
            pass

        try:
            s = LiveChatSession.objects.filter(code=room).order_by("-id").first()
            return int(s.id) if s else None
        except Exception:
            return None
        
    @database_sync_to_async
    def _is_current_session_closed(self, room: str, session_id: Optional[int] = None) -> bool:
        """
        현재 room/session_id 기준으로 상담이 종료되었는지 확인.
        종료된 상담이면 이후 메시지 저장/전송을 막기 위해 사용.
        """
        if LiveChatSession is None:
            return False

        try:
            session = None

            if session_id:
                session = LiveChatSession.objects.filter(id=session_id).first()

            if session is None and room:
                session = LiveChatSession.objects.filter(room=room).order_by("-id").first()

            return _is_session_closed_obj(session)
        except Exception:
            log.warning("check livechat closed failed", exc_info=True)
            return False

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return

        try:
            raw = json.loads(text_data)
        except Exception:
            raw = {}

        data = _unwrap_payload(raw)

        room = _pick_room(data, fallback_room=str(getattr(self, "room", "unknown")))
        session_id = _to_int(_pick_first(data, "session_id", "sessionId", "sid", default=None))
        if session_id is None and room and LiveChatSession is not None:
            session_id = await self._resolve_session_id_by_room(room)

        sender_raw = _pick_sender_raw(data)
        text = _pick_text(data)
        effective_type = _pick_type(data)

        ts = _ts_ms(_pick_first(data, "ts", default=None)) or int(timezone.now().timestamp() * 1000)

        sender_norm, _trimmed = _normalize_sender_role(sender_raw, max_len=16)

        # 종료 주체와 관계없이 고객에게는 상담사가 보낸 표준 종료
        # 안내로 전달하고, 기록에도 동일한 발신자/문구를 남긴다.
        if effective_type in _END_TYPES:
            sender_norm = "operator"
            text = _STANDARD_END_MESSAGE

        # ✅ 이미 종료된 상담이면 메시지 저장/전송 자체를 차단
        if effective_type not in _END_TYPES:
            is_closed = await self._is_current_session_closed(room=room, session_id=session_id)
            if is_closed:
                msg = "이미 종료된 상담입니다. 새 상담을 요청해 주세요."
                end_payload = {
                    "type": "end",
                    "event": "session_closed",
                    "closed": True,
                    "can_send": False,
                    "sender": "system",
                    "role": "system",
                    "text": msg,
                    "content": msg,
                    "room": room,
                    "session_id": session_id,
                    "ts": int(timezone.now().timestamp() * 1000),
                }

                try:
                    await self.send(text_data=json.dumps(end_payload, ensure_ascii=False))
                except Exception:
                    pass

                _safe_log(
                    "ws_block",
                    reason="session_closed",
                    room=room,
                    sid=session_id,
                    sender=sender_norm,
                    type=effective_type,
                    len=len(text),
                )

                try:
                    await self.close(code=1000)
                except Exception:
                    pass

                return
            
        # ✅ 고객 메시지 폭주 방지
        # 연결 자체는 유지하고, 메시지만 제한한다.
        if sender_norm == "user" and effective_type not in _END_TYPES:
            rate_ok, rate_count, rate_limit = await database_sync_to_async(_check_user_message_rate)(str(room))

            if not rate_ok:
                msg = f"메시지를 너무 빠르게 보내고 있습니다. 잠시 후 다시 시도해 주세요. 현재 제한은 1분에 {rate_limit}개입니다."

                try:
                    await self.send(
                        text_data=json.dumps(
                            {
                                "type": "blocked",
                                "code": "RATE_LIMITED",
                                "sender": "system",
                                "role": "system",
                                "text": msg,
                                "message": msg,
                                "room": room,
                                "session_id": session_id,
                                "ts": int(timezone.now().timestamp() * 1000),
                            },
                            ensure_ascii=False,
                        )
                    )
                except Exception:
                    pass

                _safe_log(
                    "ws_block",
                    reason="rate_limit",
                    room=room,
                    sid=session_id,
                    count=rate_count,
                    limit=rate_limit,
                )
                return

        _safe_log(
            "ws_recv",
            room=room,
            sid=session_id,
            sender=sender_norm,
            type=effective_type,
            len=len(text),
        )

        # end류는 텍스트 없는 이벤트로
        if sender_norm == "user" and effective_type in _END_TYPES:
            text = ""

        # ✅ 전송 직전 차단(user): PII/욕설·모욕/성희롱
        if sender_norm == "user" and effective_type not in _END_TYPES:
            # (1) PII
            ok, hits = _guard_text(str(text))
            if not ok:
                msg = (
                    _summarize_hits(hits)
                    or _get_setting("PII_BLOCK_MESSAGE", "개인정보로 보이는 내용은 상담사에게 전달되지 않습니다.")
                )
                try:
                    await self.send(
                        text_data=json.dumps(
                            {
                                "type": "blocked",
                                "code": "PII_BLOCKED",
                                "sender": "system",
                                "role": "system",
                                "text": msg,
                                "message": msg,
                                "room": room,
                                "session_id": session_id,
                                "ts": ts,
                            },
                            ensure_ascii=False,
                        )
                    )
                except Exception:
                    pass

                _safe_log("ws_block", reason="pii", room=room, sid=session_id, len=len(text))

                if _end_on("pii"):
                    end_text = "정책 위반(개인정보)으로 상담이 종료되었습니다. 새 상담이 필요하면 다시 요청해 주세요."
                    end_payload = {
                        "type": "end",
                        "code": "PII_END",
                        "sender": "system",
                        "role": "system",
                        "text": end_text,
                        "content": end_text,
                        "ts": ts,
                        "room": room,
                        "session_id": session_id,
                    }

                    # ✅ 고객에게 먼저 확실히 안내
                    try:
                        await self.send(text_data=json.dumps(end_payload, ensure_ascii=False))
                    except Exception:
                        pass

                    # ✅ 그룹에는 "나 제외"로 송신(중복 방지)
                    try:
                        await self.channel_layer.group_discard(self.group_name, self.channel_name)
                    except Exception:
                        pass
                    try:
                        await self.channel_layer.group_send(self.group_name, {"type": "room_message", "payload": end_payload})
                    except Exception:
                        pass

                    # ✅ 마스터 상태 이벤트도 남김(선택)
                    try:
                        await self._broadcast_master(
                            {
                                "type": "session_ended",
                                "reason": "pii",
                                "room": room,
                                "session_id": session_id,
                                "ts": int(timezone.now().timestamp() * 1000),
                            }
                        )
                    except Exception:
                        pass

                    try:
                        await self.close(code=1000)
                    except Exception:
                        pass
                return

            # (2) 성희롱
            if _match_any(_get_sexual_regexes(), str(text)):
                msg = "성희롱/부적절한 성적 표현은 전송되지 않습니다."
                try:
                    await self.send(
                        text_data=json.dumps(
                            {
                                "type": "blocked",
                                "code": "SEXUAL_BLOCKED",
                                "sender": "system",
                                "role": "system",
                                "text": msg,
                                "message": msg,
                                "room": room,
                                "session_id": session_id,
                                "ts": ts,
                            },
                            ensure_ascii=False,
                        )
                    )
                except Exception:
                    pass

                _safe_log("ws_block", reason="sexual", room=room, sid=session_id, len=len(text))

                if _end_on("sexual"):
                    end_text = "정책 위반(성희롱)으로 상담이 종료되었습니다. 새 상담이 필요하면 다시 요청해 주세요."
                    end_payload = {
                        "type": "end",
                        "code": "SEXUAL_END",
                        "sender": "system",
                        "role": "system",
                        "text": end_text,
                        "content": end_text,
                        "ts": ts,
                        "room": room,
                        "session_id": session_id,
                    }

                    try:
                        await self.send(text_data=json.dumps(end_payload, ensure_ascii=False))
                    except Exception:
                        pass

                    try:
                        await self.channel_layer.group_discard(self.group_name, self.channel_name)
                    except Exception:
                        pass
                    try:
                        await self.channel_layer.group_send(self.group_name, {"type": "room_message", "payload": end_payload})
                    except Exception:
                        pass

                    try:
                        await self._broadcast_master(
                            {
                                "type": "session_ended",
                                "reason": "sexual",
                                "room": room,
                                "session_id": session_id,
                                "ts": int(timezone.now().timestamp() * 1000),
                            }
                        )
                    except Exception:
                        pass

                    try:
                        await self.close(code=1000)
                    except Exception:
                        pass
                return

            # (3) 욕설/모욕
            if _match_any(_get_abuse_regexes(), str(text)):
                msg = "욕설/모욕 표현은 전송되지 않습니다."
                try:
                    await self.send(
                        text_data=json.dumps(
                            {
                                "type": "blocked",
                                "code": "ABUSE_BLOCKED",
                                "sender": "system",
                                "role": "system",
                                "text": msg,
                                "message": msg,
                                "room": room,
                                "session_id": session_id,
                                "ts": ts,
                            },
                            ensure_ascii=False,
                        )
                    )
                except Exception:
                    pass

                _safe_log("ws_block", reason="abuse", room=room, sid=session_id, len=len(text))

                if _end_on("abuse"):
                    end_text = "정책 위반(욕설/모욕)으로 상담이 종료되었습니다. 새 상담이 필요하면 다시 요청해 주세요."
                    end_payload = {
                        "type": "end",
                        "code": "ABUSE_END",
                        "sender": "system",
                        "role": "system",
                        "text": end_text,
                        "content": end_text,
                        "ts": ts,
                        "room": room,
                        "session_id": session_id,
                    }

                    try:
                        await self.send(text_data=json.dumps(end_payload, ensure_ascii=False))
                    except Exception:
                        pass

                    try:
                        await self.channel_layer.group_discard(self.group_name, self.channel_name)
                    except Exception:
                        pass
                    try:
                        await self.channel_layer.group_send(self.group_name, {"type": "room_message", "payload": end_payload})
                    except Exception:
                        pass

                    try:
                        await self._broadcast_master(
                            {
                                "type": "session_ended",
                                "reason": "abuse",
                                "room": room,
                                "session_id": session_id,
                                "ts": int(timezone.now().timestamp() * 1000),
                            }
                        )
                    except Exception:
                        pass

                    try:
                        await self.close(code=1000)
                    except Exception:
                        pass
                return

        payload = {
            "type": effective_type,
            "sender": sender_raw,  # 화면 표시용(원문)
            "role": sender_norm,  # ✅ 표준 role
            "text": text,
            "content": text,  # ✅ 프론트 호환
            "ts": ts,
            "room": room,
            "session_id": session_id,
        }

        # 1) 룸 브로드캐스트 (화면 표시)
        try:
            await self.channel_layer.group_send(self.group_name, {"type": "room_message", "payload": payload})
        except Exception as e:
            _safe_log("ws_warn", reason="group_send_failed", room=room, sid=session_id, err=str(e)[:200])
            log.warning("group_send failed: %s", e)

        # 2) ✅ DB 저장 (store.py가 operator 저장 마스킹 담당)
        try:
            persist = _boolish(_get_setting("LIVECHAT_PERSIST_MESSAGES", True), default=True)
            if persist:
                msg_id = await database_sync_to_async(save_ws_message)(
                    room=room,
                    session_id=session_id,
                    sender_norm=sender_norm,
                    effective_type=effective_type,
                    body=str(text),
                    ts=ts,
                )
                _safe_log(
                    "save_ok" if msg_id else "save_skip",
                    room=room,
                    sid=session_id,
                    sender=sender_norm,
                    type=effective_type,
                    len=len(text),
                    msg_id=msg_id,
                )
            else:
                _safe_log("save_skip", reason="persist_off", room=room, sid=session_id)
        except Exception as e:
            _safe_log("save_fail", room=room, sid=session_id, err_cls=e.__class__.__name__, err=str(e)[:200])

        # 3) master 브로드캐스트(상태 이벤트)
        if sender_norm == "operator" and effective_type not in _END_TYPES:
            try:
                await self._broadcast_master(
                    {
                        "type": "session_started",
                        "room": room,
                        "session_id": session_id,
                        "ts": int(timezone.now().timestamp() * 1000),
                    }
                )
            except Exception:
                pass

        if effective_type in _END_TYPES:
            try:
                await self._broadcast_master(
                    {
                        "type": "session_ended",
                        "room": room,
                        "session_id": session_id,
                        "ts": int(timezone.now().timestamp() * 1000),
                    }
                )
            except Exception:
                pass
            try:
                await self.close(code=1000)
            except Exception:
                pass

    async def room_message(self, event: Dict[str, Any]):
        payload = event.get("payload") or {}

        try:
            await self.send(text_data=json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            log.warning("RoomConsumer send failed: %s", e)
            return

        # ✅ 상담 종료 이벤트를 받으면 고객 WebSocket도 닫음
        try:
            data = _unwrap_payload(payload)
            msg_type = _pick_type(data)
            event_type = str(data.get("event") or "").strip().lower()

            should_close = (
                msg_type in _END_TYPES
                or event_type == "session_closed"
                or data.get("closed") is True
                or data.get("can_send") is False
            )

            if should_close:
                await self.close(code=1000)
        except Exception:
            pass
