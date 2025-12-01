# ragapp/livechat/consumers.py
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional, Tuple

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils import timezone

from ragapp.livechat import agent_api

log = logging.getLogger(__name__)

# (프로젝트에 이미 있는 모델 기준으로 import 시도)
try:
    from ragapp.models import LiveChatSession  # type: ignore
except Exception:  # pragma: no cover
    LiveChatSession = None  # type: ignore

# ✅ LiveChatMessage는 “단일 출처”를 우선 사용 (충돌 방지 + history API 정합성)
#    - models_chat_retention 쪽이 실제 저장용이면 여기로 고정
try:
    from ragapp.models_chat_retention import LiveChatMessage  # type: ignore
except Exception:  # pragma: no cover
    try:
        from ragapp.models import LiveChatMessage  # type: ignore
    except Exception:  # pragma: no cover
        LiveChatMessage = None  # type: ignore

# 🔹 욕설/보관 관련 추가 (있으면 쓰고, 없으면 조용히 비활성)
try:
    from ragapp.models_chat_retention import (  # type: ignore
        ChatEvidence,
        RetentionClass,
        compute_purge_at,
        LiveChatAbuseKeyword,
    )
except Exception:  # pragma: no cover
    ChatEvidence = None            # type: ignore
    RetentionClass = None          # type: ignore
    compute_purge_at = None        # type: ignore
    LiveChatAbuseKeyword = None    # type: ignore


# ─────────────────────────────────────
#  욕설/모욕 자동 감지 (DB + fallback)
# ─────────────────────────────────────

# 기본 fallback 키워드 (DB 비어있을 때만 의미 있음)
STATIC_ABUSE_KEYWORDS = [
    "씨발",
    "씹년",
    "병신",
    "지랄",
    "개새끼",
    "꺼져",
    "죽여버린다",
    "fuck",
    "bitch",
    "asshole",
]

# ✅ 자동 인사말 텍스트 (한 세션에서 한 번만 허용)
AUTO_GREETING_TEXT = (
    "상담사가 연결되었습니다. 안녕하세요, 김동건 포트폴리오 실시간 상담입니다. 무엇을 도와드릴까요?"
)


def _detect_abuse_flag(text: str) -> Optional[str]:
    """
    욕설/모욕 감지.
    - 1순위: LiveChatAbuseKeyword 테이블에서 is_active=True 인 패턴 검사
    - 2순위: STATIC_ABUSE_KEYWORDS 리스트로 보조 검사
    - 매칭되면 'auto:kw:<패턴>' 형태 문자열 반환
    """
    if not text:
        return None

    # 원문/소문자/공백제거 버전 모두 준비
    orig = str(text)
    t_lower = orig.lower()
    t_compact = re.sub(r"\s+", "", t_lower)

    # 1) DB 기반 금지어
    if LiveChatAbuseKeyword is not None:
        try:
            qs = LiveChatAbuseKeyword.objects.filter(is_active=True).order_by("id")
            for kw in qs:
                pattern = (kw.pattern or "").strip()
                if not pattern:
                    continue

                if kw.use_regex:
                    # 정규식 패턴
                    try:
                        r = re.compile(pattern, re.IGNORECASE)
                    except Exception:
                        # 잘못된 정규식은 그냥 무시
                        continue
                    if r.search(orig) or r.search(t_compact):
                        return f"auto:kw:{pattern}"
                else:
                    # 단순 포함 키워드
                    p = pattern.lower()
                    if p in t_lower or p in t_compact:
                        return f"auto:kw:{pattern}"
        except Exception:
            # DB 쿼리 문제 생겨도 아래 static 리스트로 계속 진행
            pass

    # 2) static 키워드 fallback
    for kw in STATIC_ABUSE_KEYWORDS:
        k = kw.lower()
        if k in t_lower or k in t_compact:
            return f"auto:kw:{kw}"

    return None


def _safe_group_name(s: str) -> str:
    out = []
    for ch in (s or ""):
        if ch.isalnum() or ch in ("_", "-", "."):
            out.append(ch)
        else:
            out.append("_")
    g = "".join(out)[:80]
    return g or "unknown"


def _model_fields(model) -> set[str]:
    try:
        # ForeignKey 등도 포함해서 name 기준으로 필드 이름 세트 반환
        return {f.name for f in model._meta.get_fields() if hasattr(f, "attname")}
    except Exception:
        return set()


def _to_int(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        if isinstance(v, bool):
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
    """
    ts는 ms(int) 기준으로 맞춤.
    - 초 단위/문자열 등 최대한 보정해서 ms 단위 int 로.
    """
    if v is None:
        return None
    try:
        if isinstance(v, int):
            # 너무 작으면(초단위 가능성) ms로 보정
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


# ─────────────────────────────────────
# MasterConsumer (운영자 로비 / 상담사 온라인 상태)
# ─────────────────────────────────────
class MasterConsumer(AsyncWebsocketConsumer):
    """
    운영자 로비(대기요청) 채널:
      ws://<host>/ws/chat/master

    - views._send_master(...) 가 group_send("livechat_master", ...) 하는 걸
      그대로 브로드캐스트.
    - 접속/해제 시 agent_api 를 통해 상담사 온라인/오프라인 카운트.
    """
    group_name = "livechat_master"

    async def connect(self):
        # 그룹 등록 + 연결
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        # 초기 연결 알림 (선택)
        try:
            await self.send(text_data=json.dumps({"type": "master_connected"}))
        except Exception:
            pass

        # ✅ 상담사 1명 온라인으로 카운트
        try:
            agent_api.mark_operator_online()
        except Exception:
            log.warning("mark_operator_online failed", exc_info=True)

    async def disconnect(self, close_code):
        # ✅ 상담사 1명 오프라인 처리
        try:
            agent_api.mark_operator_offline()
        except Exception:
            log.warning("mark_operator_offline failed", exc_info=True)

        # 그룹 제거
        try:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        except Exception:
            pass

    async def receive(self, text_data=None, bytes_data=None):
        # 마스터는 클라이언트 → 서버 메시지는 사용하지 않음
        return

    async def broadcast(self, event: Dict[str, Any]):
        """
        views._send_master(...) 에서 group_send 할 때 쓰는 핸들러
        event = {"type": "broadcast", "payload": {...}}
        """
        payload = event.get("payload") or {}
        try:
            await self.send(text_data=json.dumps(payload))
        except Exception as e:
            log.warning("MasterConsumer send failed: %s", e)


# ─────────────────────────────────────
# RoomConsumer (사용자 ↔ 상담사 개별 룸)
# ─────────────────────────────────────
class RoomConsumer(AsyncWebsocketConsumer):
    """
    개별 룸 채널:
      ws://<host>/ws/chat/<room>

    같은 room group으로 메시지 브로드캐스트.
    + DB의 session 상태(start/end)를 갱신하고 master로 이벤트를 뿌림
    + 메시지를 LiveChatMessage에 저장 (history API가 session_id로 읽는 구조를 맞춤)
    """

    async def connect(self):
        self.room = self.scope["url_route"]["kwargs"].get("room") or "unknown"
        self.group_name = "livechat_room_" + _safe_group_name(str(self.room))

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        # 클라이언트 쪽에서 연결 상태 확인용
        try:
            await self.send(
                text_data=json.dumps({"type": "room_connected", "room": self.room})
            )
        except Exception:
            pass

    async def disconnect(self, close_code):
        try:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        except Exception:
            pass

    async def _broadcast_master(self, payload: Dict[str, Any]):
        try:
            await self.channel_layer.group_send(
                "livechat_master",
                {"type": "broadcast", "payload": payload},
            )
        except Exception as e:
            log.warning("broadcast master failed: %s", e)

    # ─────────────────────────────────────────────────────────────
    # DB helpers
    # ─────────────────────────────────────────────────────────────
    def _get_session_and_fields_sync(
        self, room: str, session_id: Optional[int]
    ) -> Tuple[Optional[Any], set]:
        if LiveChatSession is None:
            return None, set()
        model = LiveChatSession
        fields = _model_fields(model)

        qs = model.objects.all()
        obj = None

        if session_id:
            obj = qs.filter(id=session_id).order_by("-id").first()
        if obj is None and room:
            obj = qs.filter(room=room).order_by("-id").first()
        return obj, fields

    @database_sync_to_async
    def _resolve_session_id_by_room(self, room: str) -> Optional[int]:
        if LiveChatSession is None:
            return None
        try:
            s = LiveChatSession.objects.filter(room=room).order_by("-id").first()
            return int(s.id) if s else None
        except Exception:
            return None

    @database_sync_to_async
    def _mark_started(self, room: str, session_id: Optional[int]) -> Optional[Dict[str, Any]]:
        obj, fields = self._get_session_and_fields_sync(room, session_id)
        if obj is None:
            return None

        changed = []
        now = timezone.now()

        if "started_at" in fields and getattr(obj, "started_at", None) is None:
            setattr(obj, "started_at", now)
            changed.append("started_at")

        if "status" in fields:
            cur = (getattr(obj, "status", "") or "").strip()
            if cur in ("waiting", "대기", ""):
                setattr(obj, "status", "active")
                changed.append("status")

        if changed:
            try:
                obj.save(update_fields=changed)
            except Exception:
                obj.save()

        started_at = getattr(obj, "started_at", None) if "started_at" in fields else None
        return {
            "id": getattr(obj, "id", None),
            "room": getattr(obj, "room", room),
            "status": getattr(obj, "status", None) if "status" in fields else None,
            "started_at": started_at.isoformat() if started_at else None,
        }

    @database_sync_to_async
    def _mark_ended(self, room: str, session_id: Optional[int]) -> Optional[Dict[str, Any]]:
        obj, fields = self._get_session_and_fields_sync(room, session_id)
        if obj is None:
            return None

        changed = []
        now = timezone.now()

        if "ended_at" in fields and getattr(obj, "ended_at", None) is None:
            setattr(obj, "ended_at", now)
            changed.append("ended_at")

        if "status" in fields:
            setattr(obj, "status", "ended")
            changed.append("status")

        if changed:
            try:
                obj.save(update_fields=changed)
            except Exception:
                obj.save()

        ended_at = getattr(obj, "ended_at", None) if "ended_at" in fields else None
        return {
            "id": getattr(obj, "id", None),
            "room": getattr(obj, "room", room),
            "status": getattr(obj, "status", None) if "status" in fields else None,
            "ended_at": ended_at.isoformat() if ended_at else None,
        }

    @database_sync_to_async
    def _session_allows_message(self, room: str, session_id: Optional[int]) -> bool:
        """
        이 세션이 '아직 진행 중인지' 확인.
        - 명확히 진행 상태(대기/진행 등)만 허용하고,
          그 외 모든 상태는 '종료된 것으로 보고' 메시지 차단.
        """
        if LiveChatSession is None:
            return True

        obj, fields = self._get_session_and_fields_sync(room, session_id)
        if obj is None:
            # 세션을 못 찾으면 더 받게 하지 말고 차단 쪽으로 보는 게 안전하지만,
            # UX를 위해 일단 허용 쪽으로 둔다.
            return True

        if "status" not in fields:
            return True

        status_raw = getattr(obj, "status", "")  # enum/str 둘 다 고려
        cur = str(status_raw or "").strip().lower()
        if not cur:
            # 상태가 비어 있으면 "종료"라고 단정하긴 애매하니 허용
            return True

        # 🔹 진행 중으로 인정할 상태들(허용 리스트)
        allowed = {
            "waiting",
            "대기",
            "pending",
            "active",
            "진행",
            "in_progress",
        }

        # enum 쓸 때는 값이 "ACTIVE", "WAITING" 같이 올 수도 있어서 보정
        cur_upper = cur.upper()
        if cur in allowed or cur_upper in {"WAITING", "PENDING", "ACTIVE", "IN_PROGRESS"}:
            return True

        # 그 외의 상태(ended, 종료, done, saved, ended_need_save 등)는 모두 "종료"로 보고 차단
        return False

    # ✅ 이 세션에 상담사 메시지가 이미 있는지 확인
    def _has_operator_message_sync(self, session_id: Optional[int]) -> bool:
        if LiveChatMessage is None or not session_id:
            return False
        try:
            fields = _model_fields(LiveChatMessage)
            qs = LiveChatMessage.objects.all()

            # 세션 기준 필터
            if "session_id" in fields:
                qs = qs.filter(session_id=session_id)
            elif "session" in fields:
                qs = qs.filter(session_id=session_id)
            else:
                return False

            # role / sender 기준으로 상담사만
            if "role" in fields:
                qs = qs.filter(role__iexact="operator")
            elif "sender" in fields:
                qs = qs.filter(sender__iexact="operator")

            return qs.exists()
        except Exception:
            return False

    @database_sync_to_async
    def _has_operator_message(self, session_id: Optional[int]) -> bool:
        return self._has_operator_message_sync(session_id)

    @database_sync_to_async
    def _save_message_if_possible(
        self,
        session_id: Optional[int],
        room: str,
        sender: str,
        msg_type: str,
        text: str,
        ts: Optional[int],
    ):
        """
        LiveChatMessage 저장 + (있으면) 자동 욕설 감지/태깅/증빙 생성
        """
        if LiveChatMessage is None:
            return

        try:
            fields = _model_fields(LiveChatMessage)
            kwargs: Dict[str, Any] = {}

            # ✅ session_id / session
            if "session_id" in fields:
                kwargs["session_id"] = session_id
            elif "session" in fields and session_id is not None:
                # FK여도 session_id 컬럼이 있을 수 있으니 그대로 사용
                kwargs["session_id"] = session_id

            # ✅ room
            if "room" in fields:
                kwargs["room"] = room

            # ✅ role / sender
            if "role" in fields:
                kwargs["role"] = sender
            elif "sender" in fields:
                kwargs["sender"] = sender

            # ✅ content / text
            if "content" in fields:
                kwargs["content"] = text
            elif "text" in fields:
                kwargs["text"] = text

            # ✅ type / msg_type
            if "msg_type" in fields:
                kwargs["msg_type"] = msg_type or "message"
            elif "type" in fields:
                kwargs["type"] = msg_type or "message"

            # ✅ ts
            if "ts" in fields:
                kwargs["ts"] = ts

            # ── 실제 메시지 저장
            msg = LiveChatMessage.objects.create(**kwargs)

            # ─────────────────────────────────────
            #  자동 욕설/모욕 감지 (user 메시지만)
            # ─────────────────────────────────────
            abuse_reason: Optional[str] = None
            if sender and str(sender).lower() == "user":
                abuse_reason = _detect_abuse_flag(text)

            # 감지된 경우에만 보존 클래스/플래그 설정
            if abuse_reason and RetentionClass is not None:
                try:
                    # RetentionClass.ABUSE 값 가져오기 (TextChoices or 단순 상수 대응)
                    if hasattr(RetentionClass, "ABUSE"):
                        rc_member = RetentionClass.ABUSE
                        rc_value = getattr(rc_member, "value", rc_member)
                    else:
                        rc_value = "ABUSE"

                    changed_fields = []

                    if "retention_class" in fields:
                        msg.retention_class = rc_value
                        changed_fields.append("retention_class")

                    if "flagged_at" in fields:
                        from django.utils import timezone as _tz  # 안전 import
                        msg.flagged_at = _tz.now()
                        changed_fields.append("flagged_at")

                    if "flag_reason" in fields:
                        msg.flag_reason = abuse_reason
                        changed_fields.append("flag_reason")

                    if "purge_at" in fields and compute_purge_at is not None:
                        msg.purge_at = compute_purge_at(
                            getattr(msg, "created_at", None),
                            msg.retention_class,
                        )
                        changed_fields.append("purge_at")

                    if changed_fields:
                        msg.save(update_fields=changed_fields)

                    # (옵션) 증빙 테이블 자동 생성
                    if ChatEvidence is not None:
                        try:
                            ce_kwargs: Dict[str, Any] = {
                                "session": getattr(msg, "session", None),
                                "message": msg,
                                "captured_text": getattr(msg, "content", text),
                                "reason": abuse_reason,
                                # created_by는 null 허용이면 생략, 아니면 여기서 에러 → 무시
                            }
                            ChatEvidence.objects.create(**ce_kwargs)
                        except Exception:
                            # 증빙 생성 실패해도 메시지 저장/태깅은 유지
                            pass

                except Exception:
                    # 태깅/증빙 쪽은 실패해도 전체 WS는 깨지지 않게
                    pass

        except Exception:
            # 최악의 경우에도 WS 자체는 계속 돌아가게 실패 삼킴
            return

    # ─────────────────────────────────────────────────────────────
    # WS receive
    # ─────────────────────────────────────────────────────────────
    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return
        try:
            data = json.loads(text_data)
        except Exception:
            data = {}

        sender = (data.get("sender") or "system")
        text = (data.get("text") or "")
        msg_type = (data.get("type") or "").lower()

        room = str(self.room or "unknown")

        # session_id 입력 형태 흡수
        session_id = _to_int(
            data.get("session_id") or data.get("sessionId") or data.get("sid")
        )

        # ✅ session_id가 비면 room 기준 최근 세션으로 보정
        if session_id is None and room and LiveChatSession is not None:
            session_id = await self._resolve_session_id_by_room(room)

        ts = _ts_ms(data.get("ts")) or int(timezone.now().timestamp() * 1000)

        s_lower = str(sender).lower()
        effective_type = msg_type or "message"

        # 🔐 이미 종료된 세션이면 user 메시지 차단
        if s_lower == "user" and effective_type not in ("end", "closed", "close"):
            try:
                allowed = await self._session_allows_message(room, session_id)
            except Exception:
                allowed = True

            if not allowed:
                # 선택: 고객 쪽에만 종료 안내 (그룹 브로드캐스트/DB 저장 X)
                try:
                    await self.send(
                        text_data=json.dumps(
                            {
                                "type": "system",
                                "code": "chat_ended",
                                "message": "이미 종료된 상담입니다. 새 상담을 다시 시작해주세요.",
                                "room": room,
                                "session_id": session_id,
                                "ts": ts,
                            }
                        )
                    )
                except Exception:
                    pass
                return

        # ✅ 한 세션에서 자동 인사말은 한 번만 허용
        is_auto_greeting = (
            s_lower == "operator"
            and isinstance(text, str)
            and text.strip() == AUTO_GREETING_TEXT.strip()
        )
        if is_auto_greeting and session_id is not None:
            try:
                already_has_operator = await self._has_operator_message(session_id)
            except Exception:
                already_has_operator = False

            if already_has_operator:
                # 이미 이 세션에 상담사 메시지가 한 번 이상 있는 경우
                # 자동 인사말은 다시 보내지 않음 (브로드캐스트/저장 둘 다 스킵)
                return

        payload = {
            "type": effective_type,
            "sender": sender,
            "text": text,
            "ts": ts,
            "room": room,
            "session_id": session_id,
        }

        # 1) 룸 그룹으로 브로드캐스트
        await self.channel_layer.group_send(
            self.group_name,
            {"type": "room_message", "payload": payload},
        )

        # 2) 메시지 저장 (+ 욕설 감지/증빙)
        try:
            await self._save_message_if_possible(
                session_id=session_id,
                room=room,
                sender=str(sender),
                msg_type=effective_type,
                text=str(text),
                ts=ts,
            )
        except Exception:
            pass

        # 3) 상태 이벤트 처리 + master로 브로드캐스트

        # 상담사 메시지로 session_started 처리
        if s_lower == "operator" and msg_type not in ("end", "closed", "close"):
            info = await self._mark_started(room, session_id)
            if info:
                await self._broadcast_master(
                    {
                        "type": "session_started",
                        "room": info.get("room") or room,
                        "session_id": info.get("id") or session_id,
                        "status": info.get("status"),
                        "started_at": info.get("started_at"),
                        "ts": int(timezone.now().timestamp() * 1000),
                    }
                )

        # end류면 종료 처리
        if msg_type in ("end", "closed", "close"):
            info = await self._mark_ended(room, session_id)
            if info:
                await self._broadcast_master(
                    {
                        "type": "session_ended",
                        "room": info.get("room") or room,
                        "session_id": info.get("id") or session_id,
                        "status": info.get("status"),
                        "ended_at": info.get("ended_at"),
                        "ts": int(timezone.now().timestamp() * 1000),
                    }
                )

            try:
                await self.close(code=1000)
            except Exception:
                pass

    async def room_message(self, event: Dict[str, Any]):
        payload = event.get("payload") or {}
        try:
            await self.send(text_data=json.dumps(payload))
        except Exception as e:
            log.warning("RoomConsumer send failed: %s", e)
