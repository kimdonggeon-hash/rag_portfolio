# ragapp/views_feedback.py
from __future__ import annotations

import json
import hashlib
import logging

from django.http import JsonResponse, HttpRequest
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt

from ragapp.models import QaragFeedback, ChatQueryLog

log = logging.getLogger(__name__)


def _hash_ip(ip: str) -> str:
    if not ip:
        return ""
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()[:64]


@require_POST
@csrf_exempt
def api_qarag_feedback(request: HttpRequest) -> JsonResponse:
    """
    질문 챗봇(QARAG) 전용 피드백 저장 API

    기대 JSON:
    {
      "helpful": true/false,
      "comment": "문장...",
      "question": "사용자 질문",
      "answer": "챗봇 답변",
      "log_id": 123,            # 선택: ChatQueryLog PK
      "session_id": "sessionid" # 선택
    }
    """
    try:
        # 1) JSON 파싱
        try:
            raw = request.body.decode("utf-8") or "{}"
            payload = json.loads(raw)
        except Exception as e:
            log.exception("QARAG_FEEDBACK_INVALID_JSON")
            return JsonResponse(
                {"ok": False, "error": "invalid_json"},
                status=400,
                json_dumps_params={"ensure_ascii": False},
            )

        helpful = payload.get("helpful", None)
        # 잘못된 값 들어오면 None 처리
        if helpful not in (True, False):
            helpful = None

        question = (payload.get("question") or "").strip()
        answer = (payload.get("answer") or "").strip()
        comment = (payload.get("comment") or "").strip()
        session_id = (payload.get("session_id") or "").strip()
        log_id = payload.get("log_id") or payload.get("chat_log_id")

        # 2) ChatQueryLog 연결(선택)
        chat_log = None
        if log_id:
            try:
                chat_log = ChatQueryLog.objects.get(pk=log_id)
            except ChatQueryLog.DoesNotExist:
                chat_log = None

        # 3) IP / UA
        ip = request.META.get("REMOTE_ADDR", "") or ""
        ua = request.META.get("HTTP_USER_AGENT", "") or ""

        # 4) 피드백 레코드 생성
        fb = QaragFeedback.objects.create(
            chat_log=chat_log,
            session_id=session_id,
            question=question,
            answer=answer,
            is_helpful=helpful,
            comment=comment,
        )

        return JsonResponse(
            {"ok": True, "id": fb.id},
            json_dumps_params={"ensure_ascii": False},
        )

    except Exception as e:
        # 🔍 디버깅용: 서버 콘솔에 전체 스택 찍기 + 클라이언트에 에러 메시지 전달
        log.exception("QARAG_FEEDBACK_ERROR")
        return JsonResponse(
            {
                "ok": False,
                "error": str(e) or e.__class__.__name__,
            },
            status=500,
            json_dumps_params={"ensure_ascii": False},
        )
