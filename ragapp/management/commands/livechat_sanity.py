# ragapp/management/commands/livechat_sanity.py
from __future__ import annotations

import os
import sys
from datetime import timedelta
from typing import Any, Dict, List, Optional

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.db.models import Count
from django.utils import timezone


def _mask(v: Optional[str]) -> str:
    if not v:
        return ""
    if len(v) <= 4:
        return "*" * len(v)
    return v[:2] + ("*" * (len(v) - 4)) + v[-2:]


def _room_candidates(room: str) -> List[str]:
    r = (room or "").strip()
    if not r:
        return []
    rr = r.rstrip("/")
    cands: List[str] = []

    if "/" in rr:
        last = rr.split("/")[-1]
        if last:
            cands.append(last)

    if ":" in rr:
        last = rr.split(":")[-1]
        if last:
            cands.append(last)

    prefixes = ("chat_", "room_", "ws_", "wschat_", "livechat_", "lc_")
    for p in prefixes:
        if rr.startswith(p):
            s = rr[len(p) :]
            if s:
                cands.append(s)

    cands.append(rr)

    out: List[str] = []
    seen = set()
    for x in cands:
        x = (x or "").strip()
        if x and x not in seen:
            out.append(x)
            seen.add(x)
    return out


def _get_msg_model():
    try:
        from ragapp.models_chat_retention import LiveChatMessage as M  # type: ignore
        return M
    except Exception:
        pass
    try:
        return apps.get_model("ragapp", "LiveChatMessage")
    except Exception:
        return None


def _get_session_model():
    try:
        return apps.get_model("ragapp", "LiveChatSession")
    except Exception:
        return None


class Command(BaseCommand):
    help = "Cloud Run에서 라이브챗 메시지 저장/DB연결/room 매칭 문제를 빠르게 판별합니다."

    def add_arguments(self, parser):
        parser.add_argument("--room", type=str, default="", help="특정 room으로 세션/메시지 매칭을 점검")
        parser.add_argument("--hours", type=int, default=24, help="최근 N시간 기준 통계 (기본 24)")
        parser.add_argument("--write-test", action="store_true", help="테스트 세션/메시지를 실제로 저장해보고 확인")
        parser.add_argument("--cleanup", action="store_true", help="--write-test로 만든 데이터 삭제")

    def handle(self, *args, **opts):
        room = (opts.get("room") or "").strip()
        hours = int(opts.get("hours") or 24)
        write_test = bool(opts.get("write_test"))
        cleanup = bool(opts.get("cleanup"))

        now = timezone.now()

        self.stdout.write("=== livechat_sanity ===")
        self.stdout.write(f"now={now.isoformat()}")
        self.stdout.write(f"python={sys.version.split()[0]}  pid={os.getpid()}")

        # store.py 경로(배포본 확인)
        try:
            from ragapp.livechat import store as st  # type: ignore
            self.stdout.write(f"store_file={getattr(st, '__file__', '')}")
            self.stdout.write(f"store_save_ws_message_line={getattr(getattr(st, 'save_ws_message', None), '__code__', None).co_firstlineno}")
        except Exception as e:
            self.stdout.write(f"store_import_failed={repr(e)}")

        # DB 연결 정보(비번은 절대 출력 X)
        try:
            sd = connection.settings_dict
            self.stdout.write(f"db_vendor={connection.vendor}")
            self.stdout.write(f"db_engine={sd.get('ENGINE')}")
            self.stdout.write(f"db_name={sd.get('NAME')}")
            self.stdout.write(f"db_host={sd.get('HOST')}")
            self.stdout.write(f"db_user={sd.get('USER')}")
        except Exception as e:
            self.stdout.write(f"db_info_failed={repr(e)}")

        # 환경변수 스냅샷(민감값 마스킹)
        env_keys = ["DB_HOST", "DB_NAME", "DB_USER", "DATABASE_URL", "CLOUDSQL_CONNECTION_NAME", "GOOGLE_CLOUD_PROJECT"]
        env_view: Dict[str, Any] = {}
        for k in env_keys:
            v = os.environ.get(k)
            if k in ("DATABASE_URL",):
                env_view[k] = _mask(v)
            else:
                env_view[k] = v
        self.stdout.write(f"env={env_view}")

        Msg = _get_msg_model()
        Sess = _get_session_model()
        if Msg is None:
            self.stdout.write("ERROR: LiveChatMessage model not found")
            return
        if Sess is None:
            self.stdout.write("ERROR: LiveChatSession model not found")
            return

        # 최근 N시간 메시지 role 통계
        since = now - timedelta(hours=hours)
        qs = Msg.objects.filter(created_at__gte=since) if hasattr(Msg, "created_at") else Msg.objects.all()
        role_field = "role" if hasattr(Msg, "role") else ("sender" if hasattr(Msg, "sender") else None)

        self.stdout.write(f"messages_since={since.isoformat()}")
        self.stdout.write(f"messages_count={qs.count()}")

        if role_field:
            rows = list(qs.values(role_field).annotate(cnt=Count("id")).order_by("-cnt"))
            self.stdout.write(f"by_role={rows}")
        else:
            self.stdout.write("by_role=SKIP (no role/sender field)")

        # 최근 메시지 샘플(내용은 길이만)
        last = (
            Msg.objects.order_by("-id")
            .values("id", "session_id", "role", "created_at", "retention_class", "purge_at")
            [:10]
        )
        self.stdout.write(f"last10={list(last)}")

        # room 점검
        if room:
            cands = _room_candidates(room)
            self.stdout.write(f"room_input={room}")
            self.stdout.write(f"room_candidates={cands}")

            sess = Sess.objects.filter(room__in=cands).order_by("-pk").first()
            if not sess:
                self.stdout.write("room_match_session=NONE  (=> room 매칭 실패 가능성 높음)")
            else:
                self.stdout.write(f"room_match_session=pk={sess.pk} room={getattr(sess,'room',None)} status={getattr(sess,'status',None)}")
                s_qs = Msg.objects.filter(session_id=int(sess.pk))
                self.stdout.write(f"session_messages_count={s_qs.count()}")
                if role_field:
                    s_rows = list(s_qs.values(role_field).annotate(cnt=Count("id")).order_by("-cnt"))
                    self.stdout.write(f"session_by_role={s_rows}")

        # write-test (원인 분리용)
        if write_test:
            try:
                from ragapp.livechat import store as st  # type: ignore
            except Exception as e:
                self.stdout.write(f"write_test_failed_store_import={repr(e)}")
                return

            test_room = f"sanity-{now.strftime('%Y%m%d-%H%M%S')}"
            created_ids: List[int] = []
            sess = None

            with transaction.atomic():
                # 최소 세션 생성
                fields = {f.name for f in Sess._meta.fields}
                sess = Sess(room=test_room)
                if "status" in fields:
                    setattr(sess, "status", "active")
                if "created_at" in fields and not getattr(sess, "created_at", None):
                    setattr(sess, "created_at", now)
                if "started_at" in fields and not getattr(sess, "started_at", None):
                    setattr(sess, "started_at", now)
                sess.save()

                # store 경유 저장(진짜 저장이 되는지)
                for role in ("system", "user", "operator"):
                    mid = st.save_ws_message(
                        room=test_room,
                        session_id=int(sess.pk),
                        sender_norm=role,
                        effective_type="message",
                        body=f"[sanity] {role} message",
                        ts=None,
                    )
                    if mid:
                        created_ids.append(int(mid))

            self.stdout.write(f"write_test_session_pk={getattr(sess,'pk',None)} room={test_room}")
            self.stdout.write(f"write_test_message_ids={created_ids}")

            # DB에서 다시 조회
            chk = list(Msg.objects.filter(id__in=created_ids).values("id", "session_id", "role", "created_at"))
            self.stdout.write(f"write_test_db_readback={chk}")

            # cleanup
            if cleanup and sess is not None:
                Msg.objects.filter(session_id=int(sess.pk), content__startswith="[sanity]").delete()
                sess.delete()
                self.stdout.write("write_test_cleanup=OK")
