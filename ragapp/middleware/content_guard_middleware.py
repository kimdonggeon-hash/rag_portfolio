from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Optional

from django.conf import settings
from django.http import JsonResponse, HttpResponse

from ragapp.machine.content_guard import detect_block_hit, GuardHit

log = logging.getLogger("ragapp.content_guard")


class ContentGuardMiddleware:
    """
    욕설/모욕/성희롱/음란 표현 포함 요청은 뷰 실행 전에 차단.
    - 임베딩/RAG/웹호출 전에 컷 → 비용/리소스 절약
    - 프론트는 200(정상) 응답으로 받아 “강력 경고 문구”를 표시하도록 유도
    """

    DEFAULT_ALLOW_PREFIXES = (
        "/static/",
        "/legal/",
        "/favicon.ico",
        "/healthz",
    )

    # 요청에서 “유저 입력”으로 의심되는 키들(너가 이미 쓰던 그대로)
    CANDIDATE_KEYS = (
        "q", "query", "question", "text", "message", "prompt", "input",
        "user_query", "userQuery", "content",
    )

    MAX_BODY_BYTES = 16_384

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not getattr(settings, "CONTENT_GUARD_ENABLED", True):
            return self.get_response(request)

        path = request.path or "/"

        allow_prefixes = getattr(settings, "CONTENT_GUARD_ALLOW_PREFIXES", None) or self.DEFAULT_ALLOW_PREFIXES
        if any(path.startswith(p) for p in allow_prefixes):
            return self.get_response(request)

        merged = "\n".join(self._extract_text_candidates(request)).strip()
        if merged:
            hit = detect_block_hit(merged)
            if hit:
                # ✅ 내용 원문은 로그에 남기지 말고(악질에게 힌트), 코드/디테일만 남김
                log.info("blocked: code=%s detail=%s path=%s", hit.code, hit.detail, path)
                return self._blocked_response(request, hit)

        return self.get_response(request)

    def _blocked_response(self, request, hit: GuardHit):
        wants_json = (
            "application/json" in (request.headers.get("Accept") or "").lower()
            or (request.headers.get("X-Requested-With") == "XMLHttpRequest")
            or (request.content_type or "").lower().startswith("application/json")
            or (request.path or "").startswith("/api/")
        )

        # ✅ 200 유지(프론트가 “정상 응답”처럼 받아 경고 표시하기 좋음)
        status = getattr(settings, "CONTENT_GUARD_BLOCK_STATUS", 200)

        # ✅ 문구는 detect_block_hit의 user_msg를 1순위로 사용 (가장 정확/일관)
        msg: str = (hit.user_msg or "").strip()

        # 그래도 비어 있으면 settings fallback
        if not msg:
            if hit.code == "sexual":
                msg = getattr(settings, "CONTENT_GUARD_MESSAGE_SEXUAL", "🚫 부적절한 표현은 허용되지 않습니다.")
            else:
                msg = getattr(settings, "CONTENT_GUARD_MESSAGE_ABUSE", "🚫 부적절한 표현은 허용되지 않습니다.")

        # ✅ 프론트(news.js)가 바로 인지할 수 있게 “통일 코드”를 사용
        # - WEB: code === SAFETY_BLOCKED 감지 가능
        # - RAG: mode === blocked 감지 가능
        payload = {
            "ok": True,                     # ✅ throw 방지(경고를 정상 플로우에서 처리)
            "blocked": True,
            "mode": "blocked",              # ✅ RAG쪽 기존 체크와 호환
            "code": "SAFETY_BLOCKED",       # ✅ WEB쪽 기존 체크와 호환
            "hit": hit.code,                # abuse | sexual (디버깅/통계용)
            "detail": hit.detail,           # 내부용(짧게)
            "message": msg,                 # 메시지 후보
            "error": msg,                   # 레거시/호환
            "answer_text": msg,             # pickAnswer()로 바로 잡히게
        }

        if wants_json:
            resp = JsonResponse(payload, status=status)
        else:
            resp = HttpResponse(msg, status=status, content_type="text/plain; charset=utf-8")

        # ✅ 디버깅/프론트 처리 힌트 헤더
        resp["X-Content-Blocked"] = "1"
        resp["X-Content-Blocked-Code"] = hit.code          # abuse|sexual
        resp["X-Content-Blocked-Mode"] = "blocked"         # 고정
        resp["Cache-Control"] = "no-store"                 # 중간 캐시 방지(혹시 몰라)
        return resp

    def _extract_text_candidates(self, request) -> Iterable[str]:
        # 1) GET 파라미터
        if request.method == "GET":
            for k in self.CANDIDATE_KEYS:
                v = request.GET.get(k)
                if isinstance(v, str) and v.strip():
                    yield v.strip()
            return

        # 2) POST/PUT/PATCH/DELETE
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            ctype = (request.content_type or "").lower()

            # 2-1) form / multipart는 body raw 디코딩 대신 request.POST에서 후보키만
            if ctype.startswith("application/x-www-form-urlencoded") or ctype.startswith("multipart/form-data"):
                try:
                    for k in self.CANDIDATE_KEYS:
                        v = request.POST.get(k)
                        if isinstance(v, str) and v.strip():
                            yield v.strip()[:4096]
                except Exception:
                    # 파싱 실패 시에는 조용히 넘어감(업로드 깨질 위험 방지)
                    return
                return

            # 2-2) JSON이면 파싱해서 문자열만 추출
            if ctype.startswith("application/json"):
                raw = (request.body or b"")[: self.MAX_BODY_BYTES]
                if not raw:
                    return
                try:
                    payload = json.loads(raw.decode("utf-8", errors="ignore"))
                except Exception:
                    # JSON 파싱 실패면 그냥 raw를 텍스트로 한번만 스캔
                    yield raw.decode("utf-8", errors="ignore")
                    return

                for v in self._walk_json(payload):
                    if isinstance(v, str) and v.strip():
                        yield v.strip()[:4096]
                return

            # 2-3) 그 외는 앞부분만 텍스트로 스캔(최후의 보험)
            raw = (request.body or b"")[: self.MAX_BODY_BYTES]
            if raw:
                yield raw.decode("utf-8", errors="ignore")

    def _walk_json(self, obj: Any):
        """
        JSON 전체를 훑되,
        - 후보키(CANDIDATE_KEYS)에 걸리는 값은 무조건 yield
        - 나머지도 하위로 내려가면서 문자열이 나오면 yield (우회 키 방지)
        """
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(k, str) and k in self.CANDIDATE_KEYS:
                    yield v
                yield from self._walk_json(v)
        elif isinstance(obj, list):
            for it in obj:
                yield from self._walk_json(it)
        else:
            yield obj
