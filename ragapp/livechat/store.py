# ragapp/livechat/store.py
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Set, Tuple, List

from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.db import transaction, connection
from django.db.models import Q
from django.utils import timezone

# ✅ PII guard (저장 시 마스킹용)
try:
    from ragapp.pii import guard_text, summarize_hits  # type: ignore
except Exception:
    guard_text = None  # type: ignore
    summarize_hits = None  # type: ignore

log = logging.getLogger(__name__)

_END_TYPES = {"end", "closed", "close"}

# ─────────────────────────────────────────────────────────────
# Base utils
# ─────────────────────────────────────────────────────────────

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


def _safe_room(room: str) -> str:
    return (room or "").strip()


def _room_candidates(room: str) -> List[str]:
    """
    클라우드에서 room 값이 group name / url 형태로 섞여 들어오는 경우가 있어서
    세션 매칭을 위해 후보를 여러 개 만든다.

    예)
      "chat_1813" / "chat:1813" / "/ws/chat/1813" / "1813/"
    """
    r = _safe_room(room)
    cands: List[str] = []
    if not r:
        return cands

    # 1) url/path 마지막 segment
    rr = r.rstrip("/")
    if "/" in rr:
        last = rr.split("/")[-1]
        if last:
            cands.append(last)

    # 2) ":" 마지막 segment
    if ":" in rr:
        last = rr.split(":")[-1]
        if last:
            cands.append(last)

    # 3) prefix strip
    prefixes = ("chat_", "room_", "ws_", "wschat_", "livechat_", "lc_")
    for p in prefixes:
        if rr.startswith(p):
            s = rr[len(p):]
            if s:
                cands.append(s)

    # 4) original
    cands.append(rr)

    # dedupe preserve order
    out: List[str] = []
    seen: Set[str] = set()
    for x in cands:
        x = (x or "").strip()
        if not x:
            continue
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out


def _normalize_role(v: str) -> str:
    """
    저장 시 role을 최대한 user/operator/system으로 정규화.
    (클라우드에서 role이 'assistant' 등으로 들어와도 이후 조회/템플릿에서 깨지지 않게)
    """
    r = (v or "").strip().lower()
    if r in ("user", "customer", "client", "human"):
        return "user"
    if r in ("operator", "master", "admin", "staff", "assistant", "bot", "ai", "model"):
        return "operator"
    if r in ("system",):
        return "system"
    return r or "system"


def _model_fields(model) -> Set[str]:
    """
    name + attname 둘 다 수집.
    FK면 session / session_id 둘 다 잡히게.
    """
    out: Set[str] = set()
    try:
        for f in model._meta.get_fields():
            n = getattr(f, "name", None)
            a = getattr(f, "attname", None)
            if n:
                out.add(str(n))
            if a:
                out.add(str(a))
    except Exception:
        pass
    return out


# ─────────────────────────────────────────────────────────────
# Model getters (runtime-safe)
# ─────────────────────────────────────────────────────────────

_SESSION_MODEL = None  # cache only on success


def _get_session_model():
    """
    LiveChatSession을 런타임에 안전하게 가져온다.
    (Cloud Run에서 import 순서/앱 레지스트리 타이밍 이슈로 None 고정되는 것 방지)
    """
    global _SESSION_MODEL
    if _SESSION_MODEL is not None:
        return _SESSION_MODEL
    try:
        m = apps.get_model("ragapp", "LiveChatSession")
        if m is not None:
            _SESSION_MODEL = m
        return m
    except Exception:
        return None


def _get_message_model():
    """
    메시지 모델: retention 우선
    """
    try:
        from ragapp.models_chat_retention import LiveChatMessage as M  # type: ignore
        return M
    except Exception:
        pass

    for name in ("LiveChatMessage", "LiveChatMsg", "ChatMessage", "LiveChatLog"):
        try:
            m = apps.get_model("ragapp", name)
            if m:
                return m
        except Exception:
            continue
    return None


def _get_room_model():
    """
    LiveChatRoom이 있으면 last_question/status 업데이트에 사용
    """
    try:
        return apps.get_model("ragapp", "LiveChatRoom")
    except Exception:
        return None


def _get_evidence_model():
    """
    ChatEvidence (있으면 ABUSE 증빙 스냅샷 저장)
    """
    try:
        return apps.get_model("ragapp", "ChatEvidence")
    except Exception:
        return None


def _find_session_fk(model) -> Tuple[Optional[str], Optional[str]]:
    """
    model 내에서 LiveChatSession을 가리키는 FK 필드를 탐지.
    반환: (fk_name, fk_attname)  예: ("session", "session_id")
    """
    try:
        for f in model._meta.get_fields():
            if not getattr(f, "is_relation", False):
                continue
            if not getattr(f, "many_to_one", False):
                continue
            rm = getattr(getattr(f, "remote_field", None), "model", None)
            if rm is None:
                continue
            label = getattr(getattr(rm, "_meta", None), "label_lower", "") or ""
            if label.endswith("livechatsession"):
                return getattr(f, "name", None), getattr(f, "attname", None)
    except Exception:
        pass
    return None, None


def _pick_text_field(mf: Set[str]) -> Optional[str]:
    for k in ("content", "text", "body", "message"):
        if k in mf:
            return k
    return None


def _pick_role_field(mf: Set[str]) -> Optional[str]:
    if "role" in mf:
        return "role"
    if "sender" in mf:
        return "sender"
    return None


def _pick_type_field(mf: Set[str]) -> Optional[str]:
    if "msg_type" in mf:
        return "msg_type"
    if "type" in mf:
        return "type"
    return None


# ─────────────────────────────────────────────────────────────
# Session ensure
# ─────────────────────────────────────────────────────────────

def _ensure_session_for_room(room: str, session_id: Optional[int]) -> Tuple[Optional[object], Optional[int]]:
    """
    session FK NOT NULL 방지
    - session_id가 있으면 pk로 조회
    - 없으면 room 후보들로 조회
    - 없으면 최소 세션 생성(로그 저장이 절대 죽지 않게)
    """
    LiveChatSession = _get_session_model()
    if LiveChatSession is None:
        return None, None

    # 1) pk로 조회
    if session_id is not None:
        try:
            s = LiveChatSession.objects.filter(pk=int(session_id)).first()
            if s:
                return s, int(s.pk)
        except Exception:
            pass

    cands = _room_candidates(room)
    # room이 정말 master/unknown 뿐이면 세션 만들지 않음(모든 상담이 master에 섞이는 사고 방지)
    if not cands:
        return None, None
    if all(c in ("unknown", "master") for c in cands):
        return None, None

    # 2) room 후보로 조회
    try:
        s = LiveChatSession.objects.filter(room__in=cands).order_by("-pk").first()
        if s:
            return s, int(s.pk)
    except Exception:
        pass

    # 3) 없으면 최소 생성 (첫 후보를 기준 room으로 사용)
    try:
        fields = _model_fields(LiveChatSession)
        base_room = next((c for c in cands if c not in ("unknown", "master")), cands[0])
        s = LiveChatSession(room=base_room)  # type: ignore[call-arg]

        now = timezone.now()
        if "status" in fields:
            setattr(s, "status", "active")
        if "created_at" in fields and not getattr(s, "created_at", None):
            setattr(s, "created_at", now)
        if "started_at" in fields and not getattr(s, "started_at", None):
            setattr(s, "started_at", now)

        s.save()

        # 디버그: 자동 생성이 계속 나면 room 매칭이 깨져있다는 뜻
        try:
            if _boolish(getattr(settings, "LIVECHAT_DEBUG_LOG", False), default=False):
                log.warning("auto-created LiveChatSession pk=%s room=%s", s.pk, str(base_room)[:32])
        except Exception:
            pass

        return s, int(s.pk)
    except Exception:
        log.warning("auto-create LiveChatSession failed (ignored)", exc_info=True)
        return None, None


# ─────────────────────────────────────────────────────────────
# Abuse rules
# ─────────────────────────────────────────────────────────────

def _load_abuse_rules() -> List[Tuple[str, bool]]:
    ck = "livechat_abuse_rules:v2"
    cached = cache.get(ck)
    if cached is not None:
        return cached

    rules: List[Tuple[str, bool]] = []

    # 1) DB 룰
    try:
        K = apps.get_model("ragapp", "LiveChatAbuseKeyword")
        if K:
            qs = K.objects.filter(is_active=True).values_list("pattern", "use_regex")
            for pat, use_regex in qs:
                p = (pat or "").strip()
                if p:
                    rules.append((p, bool(use_regex)))
    except Exception:
        rules = []

    # 2) settings fallback
    if not rules:
        kws = (
            getattr(settings, "ABUSE_KEYWORDS", None)
            or getattr(settings, "LIVECHAT_ABUSE_KEYWORDS", None)
            or []
        )
        if isinstance(kws, (list, tuple)):
            for kw in kws:
                k = (str(kw) or "").strip()
                if k:
                    rules.append((k, False))

    # 3) 최후 fallback
    if not rules:
        rules = [
            ("씨발", False), ("씹년", False), ("병신", False), ("지랄", False), ("개새끼", False), ("꺼져", False),
            ("fuck", False), ("bitch", False), ("asshole", False),
        ]

    cache.set(ck, rules, 60)
    return rules


def _is_abuse(text: str) -> bool:
    t = (text or "")
    if not t.strip():
        return False
    low = t.lower()
    for pat, use_regex in _load_abuse_rules():
        try:
            if use_regex:
                if re.search(pat, t, flags=re.IGNORECASE):
                    return True
            else:
                if str(pat).lower() in low:
                    return True
        except Exception:
            continue
    return False


# ─────────────────────────────────────────────────────────────
# Room touch
# ─────────────────────────────────────────────────────────────

def ensure_room_row(room: str) -> None:
    Room = _get_room_model()
    if Room is None:
        return

    try:
        rf = _model_fields(Room)
        if "room_id" not in rf:
            return

        defaults: Dict[str, Any] = {}
        if "client_label" in rf:
            defaults["client_label"] = "waiting"
        if "last_question" in rf:
            defaults["last_question"] = ""
        if "status" in rf:
            defaults["status"] = "waiting"

        Room.objects.get_or_create(room_id=str(_safe_room(room)), defaults=defaults)
    except Exception:
        log.warning("ensure_room_row failed (ignored)", exc_info=True)


def _touch_room_after_message(room: str, sender_norm: str, body: str, effective_type: str) -> None:
    Room = _get_room_model()
    if Room is None:
        return

    try:
        rf = _model_fields(Room)
        if "room_id" not in rf:
            return

        upd: Dict[str, Any] = {}

        if "updated_at" in rf:
            upd["updated_at"] = timezone.now()

        if "client_label" in rf and sender_norm:
            upd["client_label"] = sender_norm

        if "last_question" in rf and sender_norm == "user" and body:
            upd["last_question"] = str(body)[:500]

        if "status" in rf:
            upd["status"] = "ended" if (effective_type in _END_TYPES) else "active"

        if not upd:
            return

        rid = str(_safe_room(room))
        qs = Room.objects.filter(room_id=rid)
        updated = qs.update(**upd)
        if updated == 0:
            defaults: Dict[str, Any] = {}
            if "client_label" in rf:
                defaults["client_label"] = upd.get("client_label", "waiting")
            if "last_question" in rf:
                defaults["last_question"] = upd.get("last_question", "")
            if "status" in rf:
                defaults["status"] = upd.get("status", "waiting")
            Room.objects.get_or_create(room_id=rid, defaults=defaults)
    except Exception:
        log.warning("touch_room_after_message failed (ignored)", exc_info=True)


def _ts_to_dt(ts: Optional[int]) -> Optional[datetime]:
    if ts is None:
        return None
    try:
        tz = timezone.get_current_timezone()
        return datetime.fromtimestamp(int(ts) / 1000.0, tz=tz)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# Save WS message
# ─────────────────────────────────────────────────────────────

def save_ws_message(
    *,
    room: str,
    session_id: Optional[int],
    sender_norm: str,
    effective_type: str,
    body: str,
    ts: Optional[int] = None,
) -> Optional[int]:
    persist = _boolish(getattr(settings, "LIVECHAT_PERSIST_MESSAGES", True), default=True)
    if not persist:
        return None

    Msg = _get_message_model()
    if Msg is None:
        return None

    # (클라우드 원인 추적용) 1시간에 1번만: 어떤 파일/DB vendor인지 확인
    try:
        if cache.add("dbg:livechat_store_once:v2", True, 3600):
            log.warning(
                "LIVECHAT_STORE dbg | file=%s | msg_model=%s | db_vendor=%s",
                __file__,
                getattr(Msg, "_meta", None).label if Msg else None,
                getattr(connection, "vendor", None),
            )
    except Exception:
        pass

    mf = _model_fields(Msg)
    fk_name, fk_att = _find_session_fk(Msg)

    # role 정규화
    sender_norm2 = _normalize_role(sender_norm or "system")
    effective_type2 = (effective_type or "message").strip().lower()

    # ✅ 운영자 메시지: PII면 저장만 마스킹/대체 (전송/화면은 consumer에서 그대로)
    body_to_save = str(body or "")
    if sender_norm2 in ("operator", "system") and body_to_save:
        try:
            if callable(guard_text):
                ok, hits = guard_text(body_to_save)
                if not ok:
                    reason = summarize_hits(hits) if callable(summarize_hits) else ""
                    body_to_save = "[PII REDACTED] " + (reason or "")
        except Exception:
            # 가드가 터져도 PII 유출 방지 쪽으로
            body_to_save = "[PII REDACTED]"

    # 디버그(내용은 찍지 않음)
    if _boolish(getattr(settings, "LIVECHAT_DEBUG_LOG", False), default=False):
        try:
            log.info(
                "save_ws_message in | room=%s | session_id=%s | role=%s | type=%s | body_len=%s",
                str(_safe_room(room))[:48],
                session_id,
                sender_norm2,
                effective_type2,
                len(body_to_save or ""),
            )
        except Exception:
            pass

    # 세션 보정
    sess_obj, sess_id = _ensure_session_for_room(str(room or ""), session_id)

    needs_session = bool(fk_att or fk_name or ("session_id" in mf) or ("session" in mf))
    if needs_session and sess_id is None:
        log.error("save_ws_message skipped: missing session_id (room=%s)", str(_safe_room(room))[:48])
        return None

    kwargs: Dict[str, Any] = {}

    # FK 세팅 (탐지된 FK 우선)
    if fk_att and sess_id is not None:
        kwargs[fk_att] = int(sess_id)
    elif "session_id" in mf and sess_id is not None:
        kwargs["session_id"] = int(sess_id)
    elif fk_name and sess_obj is not None:
        kwargs[fk_name] = sess_obj
    elif "session" in mf and sess_obj is not None:
        kwargs["session"] = sess_obj

    if "room" in mf and room:
        kwargs["room"] = str(_safe_room(room))

    role_field = _pick_role_field(mf)
    if role_field:
        kwargs[role_field] = str(sender_norm2)[:16]

    type_field = _pick_type_field(mf)
    if type_field:
        kwargs[type_field] = str(effective_type2)[:32]

    text_field = _pick_text_field(mf)
    if text_field:
        kwargs[text_field] = str(body_to_save)

    if "created_at" in mf:
        dt = _ts_to_dt(ts) if ts else None
        if dt is not None:
            kwargs["created_at"] = dt

    if "ts" in mf and ts is not None:
        kwargs["ts"] = int(ts)

    # retention
    abuse = _is_abuse(str(body_to_save or ""))

    if "retention_class" in mf:
        kwargs["retention_class"] = "abuse" if abuse else "normal"
        if abuse:
            if "flagged_at" in mf:
                kwargs["flagged_at"] = timezone.now()
            if "flag_reason" in mf and not kwargs.get("flag_reason"):
                kwargs["flag_reason"] = "auto:keyword"
    elif "purge_at" in mf:
        default_days = int(getattr(settings, "CHAT_RETENTION_DAYS", getattr(settings, "LIVECHAT_RETENTION_DAYS_DEFAULT", 30)))
        abuse_days = int(getattr(settings, "ABUSE_RETENTION_DAYS", getattr(settings, "LIVECHAT_RETENTION_DAYS_ABUSE", 365)))
        keep_days = abuse_days if abuse else default_days
        kwargs["purge_at"] = timezone.now() + timedelta(days=keep_days)
        if "abuse_flagged" in mf:
            kwargs["abuse_flagged"] = bool(abuse)

    try:
        with transaction.atomic():
            obj = Msg.objects.create(**kwargs)

            try:
                _touch_room_after_message(str(room), str(sender_norm2), str(body_to_save or ""), str(effective_type2))
            except Exception:
                pass

            # ABUSE evidence snapshot
            try:
                if abuse and sess_id is not None:
                    Evidence = _get_evidence_model()
                    if Evidence is not None:
                        ef = _model_fields(Evidence)

                        # 중복 방지
                        try:
                            if "message" in ef and Evidence.objects.filter(message=obj).exists():
                                return int(getattr(obj, "pk", None) or 0) or None
                            if "message_id" in ef and getattr(obj, "pk", None):
                                if Evidence.objects.filter(message_id=int(obj.pk)).exists():
                                    return int(getattr(obj, "pk", None) or 0) or None
                        except Exception:
                            pass

                        e_kwargs: Dict[str, Any] = {}
                        if "session_id" in ef:
                            e_kwargs["session_id"] = int(sess_id)
                        elif "session" in ef and sess_obj is not None:
                            e_kwargs["session"] = sess_obj

                        if "message" in ef:
                            e_kwargs["message"] = obj
                        elif "message_id" in ef and getattr(obj, "pk", None):
                            e_kwargs["message_id"] = int(obj.pk)

                        if "captured_text" in ef:
                            e_kwargs["captured_text"] = str(body_to_save or "")
                        if "reason" in ef:
                            e_kwargs["reason"] = getattr(obj, "flag_reason", "") or "auto:keyword"

                        if e_kwargs.get("captured_text") is not None:
                            Evidence.objects.create(**e_kwargs)
            except Exception:
                pass

            return int(getattr(obj, "pk", None) or 0) or None

    except Exception:
        log.exception("save_ws_message failed")
        return None


# ─────────────────────────────────────────────────────────────
# Fetch
# ─────────────────────────────────────────────────────────────

def fetch_messages_for_admin(session: object, *, limit: int = 5000) -> List[Dict[str, Any]]:
    sid = getattr(session, "pk", None) or getattr(session, "id", None)
    if sid is None:
        return []
    rows = fetch_session_messages_for_log(int(sid), limit=limit)
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "role": r.get("role", "system"),
                "content": r.get("content", r.get("text", "")),
                "text": r.get("text", r.get("content", "")),
                "created_at": r.get("created_at"),
            }
        )
    return out


def fetch_session_messages_for_log(session_id: int, *, limit: int = 5000) -> List[Dict[str, Any]]:
    Msg = _get_message_model()
    if Msg is None:
        return []

    mf = _model_fields(Msg)
    fk_name, fk_att = _find_session_fk(Msg)

    qs = Msg.objects.all()

    # FK 필터
    if fk_att:
        qs = qs.filter(**{fk_att: int(session_id)})
    elif "session_id" in mf:
        qs = qs.filter(session_id=int(session_id))
    elif "session" in mf:
        qs = qs.filter(session__id=int(session_id))
    else:
        return []

    # purge_at 필터
    now = timezone.now()
    if "purge_at" in mf:
        qs = qs.filter(Q(purge_at__isnull=True) | Q(purge_at__gt=now))

    # 정렬
    if "created_at" in mf:
        qs = qs.order_by("created_at", "pk")
    elif "ts" in mf:
        qs = qs.order_by("ts", "pk")
    else:
        qs = qs.order_by("pk")

    qs = qs[: max(1, min(int(limit), 20000))]

    out: List[Dict[str, Any]] = []
    for m in qs:
        role_raw = getattr(m, "role", None) or getattr(m, "sender", None) or "system"
        role = _normalize_role(str(role_raw or "system"))
        if role not in ("user", "operator", "system"):
            role = "system"

        text = (
            getattr(m, "content", None)
            or getattr(m, "text", None)
            or getattr(m, "body", None)
            or getattr(m, "message", None)
            or ""
        )

        created_at = getattr(m, "created_at", None)
        if created_at is None and getattr(m, "ts", None):
            try:
                tz = timezone.get_current_timezone()
                created_at = datetime.fromtimestamp(int(getattr(m, "ts")) / 1000.0, tz=tz)
            except Exception:
                created_at = None

        item: Dict[str, Any] = {
            "role": role,
            "text": str(text),
            "content": str(text),
            "created_at": created_at,
        }

        if "retention_class" in mf:
            item["retention_class"] = getattr(m, "retention_class", None)
        if "purge_at" in mf:
            item["purge_at"] = getattr(m, "purge_at", None)

        if "flagged_at" in mf:
            item["flagged_at"] = getattr(m, "flagged_at", None)
        if "flag_reason" in mf:
            item["flag_reason"] = getattr(m, "flag_reason", "") or ""
        if "flagged_by" in mf:
            item["flagged_by"] = getattr(m, "flagged_by", None)

        if "abuse_flagged" in mf:
            item["abuse_flagged"] = bool(getattr(m, "abuse_flagged", False))

        out.append(item)

    return out
