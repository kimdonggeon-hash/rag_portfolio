# ragapp/admin_views.py
from __future__ import annotations

from typing import Callable, Optional, List, Dict, Any
from importlib import import_module
import os
import re
import logging
import io
import json
from pathlib import Path

from django.template import TemplateDoesNotExist
from django.contrib import messages
from django.views.decorators.cache import never_cache
from .models import FeedbackLog, FeedbackReview
from django.urls import reverse
from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.http import require_http_methods, require_POST, require_GET
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_protect
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.utils import timezone
from django.db.models import Count, Q
from ragapp.models import QaragFeedback, Feedback

from ragapp.models import LiveChatSession, ChatQueryLog  # ← 실제 모델명으로 사용
from ragapp.news_views.news_views import log_chat_message  # ✅ rag_qa_view에서 쓰던 헬퍼 재사용

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# 동적 import 유틸
# ─────────────────────────────────────────────────────────────────────
def _import_attr(dotted: str) -> Optional[Callable]:
  try:
      if ":" in dotted:
          mod_path, attr = dotted.split(":", 1)
      else:
          mod_path, attr = dotted.rsplit(".", 1)
      mod = import_module(mod_path)
      return getattr(mod, attr)
  except Exception:
      return None


def _first_impl(candidates: List[str]) -> Optional[Callable]:
  for d in candidates:
      fn = _import_attr(d)
      if callable(fn):
          return fn
  return None


# URL 정규화: HTML 붙여넣음/이상 값 → 링크 비활성화
def _normalize_url(v: object) -> str | None:
  if not v:
      return None
  s = str(v).strip()
  if "<" in s or ">" in s:  # HTML/스크립트 혼입 차단
      return None
  if s.startswith(("http://", "https://", "/", "mailto:", "tel:")):
      return s
  if re.match(r"^(www\.)?[a-z0-9.-]+\.[a-z]{2,}(/.*)?$", s, re.I):
      return "https://" + s
  return None


# ─────────────────────────────────────────────────────────────────────
# 상담 로그 조회 헬퍼
# ─────────────────────────────────────────────────────────────────────
def fetch_chat_messages(session_id: str) -> list[ChatQueryLog]:
  return list(
      ChatQueryLog.objects.filter(session_id=session_id)
      .order_by("created_at", "id")
  )


# ─────────────────────────────────────────────────────────────────────
# 실시간 콘솔 (레거시 진입점)
#   - 지금은 live_chat_view 를 메인으로 쓰고,
#     여기서는 같은 템플릿으로 넘겨주도록만 유지해도 됨.
# ─────────────────────────────────────────────────────────────────────
@staff_member_required
def live_console_view(request: HttpRequest) -> HttpResponse:
  """
  (레거시) 실시간 상담 콘솔 화면

  - ?session_id=... 없으면 최근 세션 하나 골라서 띄움
  - 내부적으로는 live_chat_view 와 같은 템플릿을 사용
  """
  session_id = (request.GET.get("session_id") or "").strip()

  if not session_id:
      # 최근 세션 하나 자동 선택 (session_id 비어있지 않은 것만)
      last_log = (
          ChatQueryLog.objects
          .exclude(session_id="")
          .order_by("-created_at")
          .first()
      )
      session_id = last_log.session_id if last_log else ""

  room = session_id or "master"
  # live_chat_view 로 리다이렉트해서 동일 UI 사용
  url = reverse("live_chat")
  return redirect(f"{url}?room={room}")


# ─────────────────────────────────────────────────────────────────────
# 운영자 콘솔에서 답변 전송 API
#   - LiveChatSession 상태 확인해서 "종료된 세션"이면 추가 전송 차단
#   - ChatQueryLog 에도 남겨서 상담기록이 어드민에 보관되도록 함
# ─────────────────────────────────────────────────────────────────────
@require_http_methods(["POST"])
@staff_member_required
@csrf_protect
def live_chat_send_view(request: HttpRequest) -> JsonResponse:
  """
  운영자가 콘솔에서 답변을 보낼 때 호출되는 API

  - 같은 ChatQueryLog 테이블에 assistant/answer 형태로 한 줄 남김
  - session_id == room 으로 맞춰서, QARAG / 콘솔이 같은 방 기준으로 로그 공유
  - LiveChatSession 이 "종료" 상태이면 추가 전송을 서버에서 막음
  """
  try:
      data = json.loads(request.body.decode("utf-8"))
  except Exception:
      return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)

  # JS 쪽에서 room 또는 session_id로 보내준다고 가정
  room = (data.get("room") or data.get("session_id") or "").strip()
  text = (data.get("text") or "").strip()

  if not room or not text:
      return JsonResponse({"ok": False, "error": "missing_params"}, status=400)

  # 🔒 여기서 '끝난 세션'이면 상담사 발송 막기
  sess_obj = None
  try:
      qs = LiveChatSession.objects.all()
      field_names = {
          f.name for f in LiveChatSession._meta.get_fields()
          if hasattr(f, "attname")
      }

      if "room" in field_names:
          sess_obj = qs.filter(room=room).order_by("-id").first()

      # room 값이 숫자(pk)일 수도 있으니 보너스로 한 번 더 시도
      if sess_obj is None and room.isdigit():
          sess_obj = qs.filter(pk=int(room)).first()
  except Exception:
      sess_obj = None

  if sess_obj is not None:
      try:
          field_names = {
              f.name for f in LiveChatSession._meta.get_fields()
              if hasattr(f, "attname")
          }

          ended = False

          # status 기반
          status = getattr(sess_obj, "status", None)
          if isinstance(status, str):
              s_norm = status.strip().lower()
              if s_norm in ("done", "종료", "ended", "closed", "완료"):
                  ended = True

          # is_active 기반
          if "is_active" in field_names:
              is_active = getattr(sess_obj, "is_active", True)
              if is_active is False:
                  ended = True

          # ended_at 기반
          if "ended_at" in field_names:
              ended_at = getattr(sess_obj, "ended_at", None)
              if ended_at:
                  ended = True

          if ended:
              # 이미 종료된 세션 → 더 이상 메시지 안 쌓고 바로 차단
              return JsonResponse(
                  {"ok": False, "error": "ended_session"},
                  status=400,
              )
      except Exception:
          # 상태 확인 중 오류가 나면, 최소한 기록은 남기되 차단은 하지 않음
          log.exception("live_chat_send_view: session ended check error")

  # ⬇️ 여기부터는 기존 로직 그대로: ChatQueryLog 에 기록
  msg = log_chat_message(
      request=request,
      session_id=room,               # 🔹 ChatQueryLog.session_id 에는 room 값을 그대로 넣어줌
      channel="live_console",        # 운영자 콘솔에서 보낸 거라 channel 구분
      mode="rag",                    # 필요하면 "gemini" 등으로 변경 가능
      role="assistant",              # 운영자/봇 → 사용자 입장에서는 assistant
      message_type="answer",
      question=f"(operator_reply to {room})",
      content=text,
      answer_excerpt=text[:300],
      sources=[],
      meta_extra={"from": "admin_console"},
  )

  return JsonResponse(
      {
          "ok": True,
          "message": {
              "id": msg.id,
              "role": msg.role,
              "message_type": msg.message_type,
              "content": msg.content,
              "created_at": msg.created_at.isoformat(),
          },
      }
  )


# ─────────────────────────────────────────────────────────────────────
# 공통 컨텍스트
# ─────────────────────────────────────────────────────────────────────
def _common_ctx(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
  base_dir = getattr(settings, "BASE_DIR", Path("."))
  ctx: Dict[str, Any] = {
      "MEDIA_URL": getattr(settings, "MEDIA_URL", "-"),
      "MEDIA_ROOT": getattr(settings, "MEDIA_ROOT", "-"),
      "VECTOR_DB_PATH": os.environ.get("VECTOR_DB_PATH")
          or str(Path(base_dir) / "vector_store.sqlite3"),
      "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),
      "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
      "AUTO_INGEST_AFTER_GEMINI": getattr(
          settings,
          "AUTO_INGEST_AFTER_GEMINI",
          os.environ.get("AUTO_INGEST_AFTER_GEMINI", "1").lower()
          not in ("0", "false", "no"),
      ),
  }
  if extra:
      ctx.update(extra)
  return ctx


# ─────────────────────────────────────────────────────────────────────
# 템플릿 키 누락 방지용 안전 기본값
# ─────────────────────────────────────────────────────────────────────
CRAWL_SAFE_DEFAULTS: Dict[str, Any] = {
  "q": "",
  "rss_q": "",
  "urls": [],
  "rss_list": [],
  "gemini_answer": "",
  "answer_text": "",
  "answer_md": "",
  "answer_html": "",
  "final_answer": "",
  "answer_sources": [],
  "sources": [],
  "ingest_results": [],
  "ingest_count": 0,
  "ingest_errors": [],
  "error": None,
  "diagnostics": {},
}

FAQ_SUGGEST_SAFE_DEFAULTS: Dict[str, Any] = {
  "candidates": [],
  "suggestions": [],
  "limit": 50,
  "error": None,
}

FAQ_PROMOTE_SAFE_DEFAULTS: Dict[str, Any] = {
  "promoted": [],
  "error": None,
}

LIVE_CHAT_SAFE_DEFAULTS: Dict[str, Any] = {"history": [], "error": None}
LEGAL_SAFE_DEFAULTS: Dict[str, Any] = {"legal_config": None, "error": None}


def _fill_answer_aliases(ctx: Dict[str, Any]) -> None:
  rep = (
      ctx.get("answer_text")
      or ctx.get("gemini_answer")
      or ctx.get("final_answer")
      or ""
  )
  for key in ("answer_text", "final_answer", "gemini_answer", "answer_md", "answer_html"):
      ctx.setdefault(key, "")
  if not ctx.get("answer_text"):
      ctx["answer_text"] = rep
  if not ctx.get("final_answer"):
      ctx["final_answer"] = rep
  if not ctx.get("answer_md"):
      ctx["answer_md"] = rep
  if not ctx.get("answer_html"):
      ctx["answer_html"] = rep


# ─────────────────────────────────────────────────────────────────────
# 1) 뉴스 크롤링
# ─────────────────────────────────────────────────────────────────────
_IMPL_CRAWL = _first_impl(
  [
      "ragapp.news_views.views_crawl:crawl_news_view",      # ✅ 너가 만든 위치
      "ragapp.news_views.views_crawl.crawl_news_view",      # ✅ 호환
      "ragapp.admin_views.crawl:crawl_news_view",
      "ragapp.admin_views.crawl.crawl_news_view",
      "ragapp.news_views.news_views:crawl_news",
      "ragapp.news_views.news_views.crawl_news",
  ]
)


@staff_member_required
@csrf_protect
def crawl_news_view(request: HttpRequest) -> HttpResponse:
  if _IMPL_CRAWL:
      return _IMPL_CRAWL(request)
  ctx = _common_ctx({"title": "뉴스 크롤링 & 인덱싱", **CRAWL_SAFE_DEFAULTS})
  _fill_answer_aliases(ctx)
  return render(request, "ragadmin/crawl_news.html", ctx)

# ─────────────────────────────────────────────────────────────────────
# 2) FAQ 추천
# ─────────────────────────────────────────────────────────────────────
_IMPL_FAQ_SUGGEST = _first_impl(
  [
      "ragapp.admin_views.faq:faq_suggest_view",
      "ragapp.admin_views.faq.faq_suggest_view",
      "ragapp.news_views.news_views:faq_suggest",
      "ragapp.news_views.news_views.faq_suggest",
  ]
)


@staff_member_required
@csrf_protect
def faq_suggest_view(request: HttpRequest) -> HttpResponse:
  if _IMPL_FAQ_SUGGEST:
      return _IMPL_FAQ_SUGGEST(request)
  ctx = _common_ctx({"title": "FAQ 추천", **FAQ_SUGGEST_SAFE_DEFAULTS})
  if ctx.get("candidates") and not ctx.get("suggestions"):
      ctx["suggestions"] = ctx["candidates"]
  return render(request, "ragadmin/faq_suggest.html", ctx)


# ─────────────────────────────────────────────────────────────────────
# 3) FAQ 승격
# ─────────────────────────────────────────────────────────────────────
_IMPL_FAQ_PROMOTE = _first_impl(
  [
      "ragapp.admin_views.faq:faq_promote_view",
      "ragapp.admin_views.faq.faq_promote_view",
  ]
)


@staff_member_required
@csrf_protect
def faq_promote_view(request: HttpRequest) -> HttpResponse:
  if _IMPL_FAQ_PROMOTE:
      return _IMPL_FAQ_PROMOTE(request)
  ctx = _common_ctx({"title": "FAQ 승격", **FAQ_PROMOTE_SAFE_DEFAULTS})
  try:
      return render(request, "ragadmin/faq_promote.html", ctx)
  except TemplateDoesNotExist:
      return HttpResponse("<h1>FAQ 승격</h1><p>템플릿이 아직 없습니다.</p>")


# ─────────────────────────────────────────────────────────────────────
# 4) 라이브 챗 (운영자 콘솔 + 오늘/최근 세션 리스트)
# ─────────────────────────────────────────────────────────────────────
_IMPL_LIVE_CHAT = _first_impl(
  [
      "ragapp.admin_views.live:live_chat_view",
      "ragapp.admin_views.live.live_chat_view",
  ]
)


@staff_member_required
@csrf_protect
def live_chat_view(request: HttpRequest) -> HttpResponse:
  """
  라이브 챗 기본 구현
  - 외부 구현(_IMPL_LIVE_CHAT)이 있으면 그쪽으로 위임
  - 없으면 ChatQueryLog + LiveChatSession 기반으로 화면 구성
  """
  # 0) 외부 구현 우선
  if _IMPL_LIVE_CHAT:
      return _IMPL_LIVE_CHAT(request)

  # 1) room / session_id / 최근 사용 방 순서로 방 결정
  room = (
      request.GET.get("room")
      or request.GET.get("session_id")
      or request.POST.get("room")
      or request.session.get("live_room")
      or "master"
  )
  room = (room or "").strip() or "master"

  # 최근 방 기억 (새로고침에도 유지)
  request.session["live_room"] = room

  # 2) ChatQueryLog 기반 대화 내역
  messages = ChatQueryLog.objects.filter(session_id=room).order_by(
      "created_at", "id"
  )

  # 3) LiveChatSession → 템플릿에서 안전하게 쓸 수 있는 dict 리스트로 변환
  try:
      field_names = {
          f.name for f in LiveChatSession._meta.get_fields()
          if hasattr(f, "attname")
      }

      qs = LiveChatSession.objects.all()
      if "created_at" in field_names:
          qs = qs.order_by("-created_at")
      elif "requested_at" in field_names:
          qs = qs.order_by("-requested_at")
      else:
          qs = qs.order_by("-id")

      raw_sessions = list(qs[:30])
  except Exception:
      raw_sessions = []

  def _first_attr(obj, *names, default=None):
      for n in names:
          if hasattr(obj, n):
              v = getattr(obj, n, None)
              if v not in (None, ""):
                  return v
      return default

  sessions: list[dict[str, Any]] = []
  for obj in raw_sessions:
      created = _first_attr(obj, "created_at", "requested_at")
      code = (
          _first_attr(obj, "code", "ticket_code", "queue_code", "short_id")
          or str(getattr(obj, "pk", ""))
      )
      note = _first_attr(obj, "session_note", "memo", "note", default="") or ""
      sess_type = _first_attr(obj, "session_type", "type", default="") or ""

      sessions.append(
          {
              "id": getattr(obj, "id", None),
              "code": code,
              "status": _first_attr(obj, "status", default="") or "",
              "room": _first_attr(obj, "room", default="") or "",
              "created_at": created,
              "session_type": sess_type,  # 예: {{ s.session_type }}
              "session_note": note,       # 예: {{ s.session_note }}
              "note": note,               # 🔴 템플릿에서 {{ s.note }} 써도 안전
              "memo": note,               # 🔴 템플릿에서 {{ s.memo }} 써도 안전
          }
      )

  # 현재 room 에 해당하는 세션(있으면)도 별도로 찾아서 내려주기
  current_session: dict[str, Any] | None = None
  for s in sessions:
      try:
          if room and (
              s.get("room") == room
              or str(s.get("id") or "") == room
              or s.get("code") == room
          ):
              current_session = s
              break
      except Exception:
          continue

  base_ctx = {"title": "라이브 챗", **LIVE_CHAT_SAFE_DEFAULTS}
  ctx = _common_ctx(base_ctx)
  ctx.update(
      {
          "room": room,
          "session_id": room,
          "initial_room": room,  # <body data-initial-room="{{ initial_room }}">
          "messages": messages,
          "sessions": sessions,  # 🔹 오늘/최근 세션 목록
          "current_session": current_session,  # 🔹 현재 방에 대한 요약 정보(있으면)
          "csp_nonce": getattr(request, "csp_nonce", None),
      }
  )
  return render(request, "ragadmin/live_chat.html", ctx)


# ─────────────────────────────────────────────────────────────────────
# 5) 오늘 세션 일괄 종료 / 개별 삭제(옵션)
# ─────────────────────────────────────────────────────────────────────
@require_http_methods(["POST"])
@staff_member_required
@csrf_protect
def live_chat_cleanup_view(request: HttpRequest) -> JsonResponse:
  """
  오늘 날짜 기준 '대기/진행' 상태 세션을 일괄 '종료'로 바꾸거나,
  (옵션) 특정 세션 하나를 삭제하는 API.

  - '오늘 세션 정리' 버튼  → { "mode": "today" } (상태만 ended 로 변경)
  - 최근 상담 세션 '삭제' → { "session_id": 123 } (그 세션만 삭제)
  """
  try:
      # JSON 우선, 폼 POST면 fallback
      try:
          payload = json.loads(request.body or "{}")
      except json.JSONDecodeError:
          payload = request.POST

      session_id = payload.get("session_id")
      mode = (payload.get("mode") or "today").strip()

      # 모델 필드들 체크 (created_at / requested_at / status / is_active / ended_at 유무 확인)
      field_names = {
          f.name for f in LiveChatSession._meta.get_fields()
          if hasattr(f, "attname")
      }

      qs = LiveChatSession.objects.all()

      # 1) 개별 삭제 모드: session_id 가 넘어온 경우 → 바로 delete
      #    (운영 환경에서는 삭제보다는 ended 처리 권장)
      if session_id:
          qs = qs.filter(pk=session_id)
          deleted_count, _ = qs.delete()
          return JsonResponse({"ok": True, "deleted": deleted_count})

      # 2) 기본: 오늘 세션 일괄 '종료' 처리
      today = timezone.localdate()
      if "created_at" in field_names:
          qs = qs.filter(created_at__date=today)
      elif "requested_at" in field_names:
          qs = qs.filter(requested_at__date=today)

      # 아직 끝나지 않은 것만 (status 필드가 있을 때)
      if "status" in field_names:
          qs = qs.exclude(status__in=["ended", "종료"])

      update_kwargs: dict = {}
      now = timezone.now()

      # status 필드가 있으면 ended 로 바꾸기
      if "status" in field_names:
          update_kwargs["status"] = "ended"

      # is_active 있으면 False
      if "is_active" in field_names:
          update_kwargs["is_active"] = False

      # ended_at 있으면 지금 시각
      if "ended_at" in field_names:
          update_kwargs["ended_at"] = now

      if update_kwargs:
          updated = qs.update(**update_kwargs)
      else:
          updated = qs.count()

      return JsonResponse({"ok": True, "updated": updated})

  except Exception as e:
      log.exception("live_chat_cleanup_view error")
      return JsonResponse({"ok": False, "error": str(e)}, status=500)


# ─────────────────────────────────────────────────────────────────────
# 6) 법적 설정
# ─────────────────────────────────────────────────────────────────────
_IMPL_LEGAL_ENTRY = _first_impl(
  [
      "ragapp.admin_views.legal:legal_config_entrypoint",
      "ragapp.admin_views.legal.legal_config_entrypoint",
  ]
)


@staff_member_required
@csrf_protect
def legal_config_entrypoint(request: HttpRequest) -> HttpResponse:
  if _IMPL_LEGAL_ENTRY:
      return _IMPL_LEGAL_ENTRY(request)

  ctx = _common_ctx({"title": "법적 설정", **LEGAL_SAFE_DEFAULTS})
  snap = _legal_config_snapshot()
  if snap is not None:
      ctx["legal_config"] = snap
  return render(request, "ragadmin/legal_config.html", ctx)



# ─────────────────────────────────────────────────────────────────────
# 5-1) 상담 기록 저장 API (세션 메모/유형/상세 기록 저장)
# ─────────────────────────────────────────────────────────────────────
@require_http_methods(["POST"])
@staff_member_required
@csrf_protect
def live_chat_save_session_view(request: HttpRequest) -> JsonResponse:
    """
    실시간 상담 콘솔 하단의 '상담 기록 저장' 버튼에서 호출하는 API.

    - room 기준으로 LiveChatSession 최신 1건을 찾아서
      session_type / session_note / memo / note / ended_at / is_active / status 등을 갱신.
    """
    try:
        # JSON 우선, 폼 POST면 fallback
        try:
            payload = json.loads(request.body or "{}")
        except json.JSONDecodeError:
            payload = request.POST

        room = (payload.get("room") or "").strip()
        session_type = (payload.get("session_type") or "").strip()
        session_note = (payload.get("session_note") or "").strip()
        session_detail = (payload.get("session_detail") or "").strip()

        if not room:
            return JsonResponse({"ok": False, "error": "missing_room"}, status=400)

        # 최소 하나는 채워져 있어야 저장
        if not (session_type or session_note or session_detail):
            return JsonResponse(
                {"ok": False, "error": "empty_session_meta"},
                status=400,
            )

        # LiveChatSession 필드들 확인
        field_names = {
            f.name for f in LiveChatSession._meta.get_fields()
            if hasattr(f, "attname")
        }

        qs = LiveChatSession.objects.all()

        # room 기준으로 우선 찾기
        if "room" in field_names:
            qs = qs.filter(room=room)

        # room 이 숫자면 pk 도 한 번 더 시도
        if room.isdigit():
            qs = qs | LiveChatSession.objects.filter(pk=int(room))

        # 최신 1건
        if "created_at" in field_names:
            qs = qs.order_by("-created_at")
        elif "requested_at" in field_names:
            qs = qs.order_by("-requested_at")
        else:
            qs = qs.order_by("-id")

        sess = qs.first()
        if not sess:
            return JsonResponse(
                {"ok": False, "error": "session_not_found"},
                status=404,
            )

        now = timezone.now()

        # 메모 텍스트 합치기 (한 줄 요약 + 상세)
        short = session_note.strip()
        detail = session_detail.strip()
        if short and detail:
            combined = f"{short}\n\n{detail}"
        elif short:
            combined = short
        else:
            combined = detail  # detail 만 있을 수도 있음

        update_kwargs: Dict[str, Any] = {}

        # 문의 유형
        if "session_type" in field_names and session_type:
            update_kwargs["session_type"] = session_type

        # 한 줄/상세 메모 → session_note / memo / note 에 공통 반영
        if combined:
            if "session_note" in field_names:
                update_kwargs["session_note"] = combined
            if "memo" in field_names:
                update_kwargs["memo"] = combined
            if "note" in field_names:
                update_kwargs["note"] = combined

        # 상태 관련 필드들
        if "status" in field_names:
            # 기존 status가 있으면 유지, 없으면 ended 로
            update_kwargs["status"] = getattr(sess, "status", None) or "ended"
        if "is_active" in field_names:
            update_kwargs["is_active"] = False
        if "ended_at" in field_names:
            update_kwargs["ended_at"] = getattr(sess, "ended_at", None) or now
        if "last_message_at" in field_names:
            # 종료 시점을 last_message_at 으로 찍어두고 싶으면 사용
            update_kwargs["last_message_at"] = getattr(sess, "last_message_at", None) or now

        if update_kwargs:
            LiveChatSession.objects.filter(pk=sess.pk).update(**update_kwargs)

        return JsonResponse(
            {
                "ok": True,
                "session_id": sess.pk,
                "room": getattr(sess, "room", room),
            }
        )
    except Exception as e:
        log.exception("live_chat_save_session_view error")
        return JsonResponse({"ok": False, "error": str(e)}, status=500)



# ─────────────────────────────────────────────────────────────────────
# LegalConfig 스냅샷 로더
#  - 필드명 자동 탐지 + ENV/Settings 폴백 + URL 정규화 + 라우트 폴백
# ─────────────────────────────────────────────────────────────────────
def _legal_config_snapshot():
  try:
      from ragapp.models import LegalConfig
  except Exception:
      return None

  # 최신/활성 1건 선택
  qs = LegalConfig.objects.all()
  for flag in ("is_active", "active", "enabled"):
      if hasattr(LegalConfig, flag):
          qs = qs.filter(**{flag: True})
          break
  for ts in ("updated_at", "modified", "created_at", "created", "id"):
      if hasattr(LegalConfig, ts):
          qs = qs.order_by(f"-{ts}")
          break
  inst = qs.first()
  if not inst:
      return None

  # 필드 normalize: 소문자+비영문자 제거 -> 값
  def _norm(s: str) -> str:
      return re.sub(r"[^a-z0-9]", "", s.lower())

  valmap: Dict[str, Any] = {}
  for f in inst._meta.get_fields():
      name = getattr(f, "name", None)
      if not name or not hasattr(inst, name):
          continue
      try:
          val = getattr(inst, name)
      except Exception:
          continue
      valmap[_norm(name)] = val

  def _pick(
      candidates=None, contains_all=None, contains_any=None, default=None
  ):
      if candidates:
          for c in candidates:
              k = _norm(c)
              if k in valmap and valmap[k] not in (None, ""):
                  return valmap[k]
      keys = list(valmap.keys())
      if contains_all:
          toks = [_norm(t) for t in contains_all]
          for k in keys:
              if all(t in k for t in toks):
                  v = valmap[k]
                  if v not in (None, ""):
                      return v
      if contains_any:
          toks = [_norm(t) for t in contains_any]
          for k in keys:
              if any(t in k for t in toks):
                  v = valmap[k]
                  if v not in (None, ""):
                      return v
      return default

  def _tobool(v):
      if isinstance(v, bool):
          return v
      if v is None:
          return False
      s = str(v).strip().lower()
      return s not in ("0", "false", "no", "off", "", "none", "null")

  snap: Dict[str, Any] = {
      "service_name": _pick(
          candidates=[
              "service_name",
              "serviceTitle",
              "service",
              "site_name",
              "sitename",
              "app_name",
          ],
          contains_any=["service", "sitename", "appname"],
      ),
      "operator_name": _pick(
          candidates=[
              "operator_name",
              "operator",
              "owner_name",
              "owner",
              "provider_name",
              "company_name",
              "corp_name",
          ],
          contains_any=["operator", "owner", "provider", "company", "corp"],
      ),
      "contact_email": _pick(
          candidates=[
              "contact_email",
              "email",
              "contact",
              "support_email",
              "admin_email",
          ],
          contains_any=["email"],
      ),
      "privacy_url": _pick(
          candidates=["privacy_url", "privacy_link", "policy_url"],
          contains_all=["privacy", "url"],
      )
      or os.environ.get("PRIVACY_URL")
      or getattr(settings, "PRIVACY_URL", None),
      "tos_url": _pick(
          candidates=["tos_url", "terms_url", "terms_link", "tos"],
          contains_any=["termsurl", "tosurl", "termslink", "tos"],
      )
      or os.environ.get("TERMS_URL")
      or getattr(settings, "TERMS_URL", None),
      "overseas_transfer_url": _pick(
          candidates=[
              "overseas_transfer_url",
              "transfer_url",
              "crossborder_url",
              "outbound_url",
          ],
          contains_any=["overseas", "transfer", "crossborder", "outbound"],
      )
      or os.environ.get("OVERSEAS_TRANSFER_URL")
      or getattr(settings, "OVERSEAS_TRANSFER_URL", None),
      "enable_consent_gate": _tobool(
          _pick(
              candidates=[
                  "enable_consent_gate",
                  "consent_gate",
                  "show_gate",
                  "gate_required",
                  "consent_required",
              ],
              contains_any=["consentgate", "consent", "gate", "agree"],
          )
      ),
      "show_footer_links": _tobool(
          _pick(
              candidates=[
                  "show_footer_links",
                  "footer_links",
                  "footer_show_links",
                  "show_footer",
                  "footer_visible",
              ],
              contains_any=["footer", "link", "visible", "show"],
          )
      ),
      "memo": _pick(
          candidates=["memo", "notes", "note", "description"], default=""
      ),
  }

  # 🔒 URL 정규화 + 폴백
  snap["privacy_url"] = _normalize_url(
      snap.get("privacy_url")
      or os.environ.get("PRIVACY_URL")
      or getattr(settings, "PRIVACY_URL", None)
  )
  snap["tos_url"] = _normalize_url(
      snap.get("tos_url")
      or os.environ.get("TERMS_URL")
      or getattr(settings, "TERMS_URL", None)
  )
  snap["overseas_transfer_url"] = _normalize_url(
      snap.get("overseas_transfer_url")
      or os.environ.get("OVERSEAS_TRANSFER_URL")
      or getattr(settings, "OVERSEAS_TRANSFER_URL", None)
  )

  # 라우트 폴백
  def _rev(name: str, default: str) -> str:
      try:
          return reverse(name)
      except Exception:
          return default

  if not snap.get("privacy_url"):
      snap["privacy_url"] = _normalize_url(
          _rev("legal_privacy", "/legal/privacy/")
      )
  if not snap.get("tos_url"):
      snap["tos_url"] = _normalize_url(_rev("legal_tos", "/legal/tos/"))
  if not snap.get("overseas_transfer_url"):
      snap["overseas_transfer_url"] = _normalize_url(
          _rev("legal_overseas", "/legal/overseas/")
      )

  # ENV가 True면 표시 토글 켜주기
  def _env_true(name: str) -> bool | None:
      val = os.environ.get(name) or getattr(settings, name, None)
      if val is None:
          return None
      s = str(val).strip().lower()
      return s not in ("0", "false", "no", "off", "", "none", "null")

  if _env_true("SHOW_FOOTER_LINKS") is True:
      snap["show_footer_links"] = True
  if _env_true("ENABLE_CONSENT_GATE") is True:
      snap["enable_consent_gate"] = True

  return snap

@staff_member_required
@require_GET
def live_chat_recent_sessions_view(request: HttpRequest) -> JsonResponse:
    """
    실시간 상담 콘솔 우측의 '최근 상담 세션' 리스트만 HTML 조각으로 반환.
    - livechat_admin.js 가 주기적으로 호출해서 session-list 내용을 갈아끼움.
    """
    try:
        field_names = {
            f.name for f in LiveChatSession._meta.get_fields()
            if hasattr(f, "attname")
        }

        qs = LiveChatSession.objects.all()

        if "created_at" in field_names:
            qs = qs.order_by("-created_at")
        elif "requested_at" in field_names:
            qs = qs.order_by("-requested_at")
        else:
            qs = qs.order_by("-id")

        sessions = list(qs[:30])

        html = render_to_string(
            "ragadmin/_live_chat_session_items.html",
            {"sessions": sessions},
            request=request,
        )

        return JsonResponse(
            {"ok": True, "html": html},
            json_dumps_params={"ensure_ascii": False},
        )
    except Exception as e:
        log.exception("live_chat_recent_sessions_view error")
        return JsonResponse(
            {"ok": False, "error": str(e)},
            status=500,
        )


from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render
from django.utils import timezone


@staff_member_required
def feedback_dashboard_view(request):
    """
    질문 챗봇(QARAG) + 웹/Gemini + RAG 피드백을
    한 눈에 보는 간단 대시보드.
    """
    today = timezone.localdate()
    start_7d = today - timezone.timedelta(days=7)

    # QARAG
    qs_qarag = QaragFeedback.objects.all()
    qs_qarag_7d = qs_qarag.filter(created_at__date__gte=start_7d)

    # Web/RAG (Feedback)
    qs_fb = Feedback.objects.all()
    qs_web = qs_fb.filter(answer_type="gemini")
    qs_rag = qs_fb.filter(answer_type="rag")

    ctx = {
        "today": today,
        "total_qarag": qs_qarag.count(),
        "total_qarag_7d": qs_qarag_7d.count(),
        "total_qarag_good_7d": qs_qarag_7d.filter(is_helpful=True).count(),
        "total_qarag_bad_7d": qs_qarag_7d.filter(is_helpful=False).count(),
        "total_web": qs_web.count(),
        "total_rag": qs_rag.count(),
        "total_web_good": qs_web.filter(is_helpful=True).count(),
        "total_rag_good": qs_rag.filter(is_helpful=True).count(),
    }
    return render(request, "ragadmin/feedback_dashboard.html", ctx)


@staff_member_required
def feedback_board_view(request):
    """
    통합 피드백 보드
    - ✅ helpful=False 인 것(조금 아쉬웠어요/별로였어요)만 모아서 본다
    - 코멘트/이유 칩을 함께 보여준다
    """

    channel = request.GET.get("channel", "all")  # all / web / rag / qa
    q = (request.GET.get("q") or "").strip()

    # 1) 기본 쿼리: 👎 인 피드백만
    qs = (
        FeedbackLog.objects.filter(helpful=False)
        .select_related("review")
        .order_by("-created_at")
    )

    # 2) 채널 필터 (웹 / RAG / 질문챗봇)
    if channel in ("web", "rag", "qa"):
        qs = qs.filter(answer_type=channel)

    # 3) 검색어 (질문 / 답변 / 코멘트 안에서 찾기)
    if q:
        qs = qs.filter(
            Q(question__icontains=q)
            | Q(answer__icontains=q)
            | Q(comment__icontains=q)
        )

    # 간단 요약 수치
    total_count = qs.count()
    today = timezone.localdate()
    today_count = qs.filter(created_at__date=today).count()

    # 페이지네이션
    paginator = Paginator(qs, 30)  # 한 페이지 30개
    page_number = request.GET.get("page") or 1
    page_obj = paginator.get_page(page_number)

    ctx = {
        "page_obj": page_obj,
        "channel": channel,
        "q": q,
        "total_count": total_count,
        "today_count": today_count,
    }
    return render(request, "ragadmin/feedback_board.html", ctx)


__all__ = [
  "crawl_news_view",
  "faq_suggest_view",
  "faq_promote_view",
  "live_chat_view",
  "live_console_view",
  "live_chat_send_view",
  "legal_config_entrypoint",
  "live_chat_cleanup_view",  # 🔹 오늘 세션 정리/개별 삭제
  "live_chat_save_session_view", # 🔹 상담 기록 저장
]