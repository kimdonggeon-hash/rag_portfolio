# ragapp/middleware/board_content_guard.py
from __future__ import annotations

import hashlib
import re
import time
from typing import Dict, List, Tuple, Any, Optional

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponseForbidden

try:
    from ragapp.board.models import BoardAbuseKeyword, BoardReport  # type: ignore
except Exception:  # pragma: no cover
    BoardAbuseKeyword = None  # type: ignore
    BoardReport = None  # type: ignore


class BoardContentGuardMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self._url_re = re.compile(r"https?://", re.I)
        self._url_extract_re = re.compile(r"https?://[^\s<>'\"\)\]]+", re.I)
        self._repeat_char_re = re.compile(r"(.)\1{14,}")
        self._laugh_re = re.compile(r"(ㅋ|ㅎ){12,}")
        self._word_repeat_re = re.compile(r"(\S+)(?:\s+\1){15,}", re.I)

    def __call__(self, request):
        if request.method == "POST" and self._is_board_path(request.path_info):
            user = getattr(request, "user", None)
            if user and getattr(user, "is_authenticated", False) and (user.is_staff or user.is_superuser):
                return self.get_response(request)

            p = request.path_info
            if "/moderate/" in p or "/mod/" in p or "/report/" in p:
                return self.get_response(request)

            fp = self._fingerprint(request)

            # ✅ ban 상태 확인 (만료시각 포함)
            ban_info = cache.get(self._ban_key(fp))
            if ban_info:
                left = self._ban_seconds_left(ban_info)
                if left is not None and left > 0:
                    mins = max(1, left // 60)
                    return self._deny(f"현재 게시판 작성이 제한된 상태입니다. (남은 약 {mins}분)")
                return self._deny("현재 게시판 작성이 제한된 상태입니다. 잠시 후 다시 시도해 주세요.")

            reason, ban_points = self._inspect(request)

            if reason:
                if ban_points > 0:
                    score, threshold = self._bump_ban_score(fp, ban_points)
                    if score >= threshold:
                        ttl = self._ban_ttl()
                        until = int(time.time()) + ttl

                        cache.set(self._ban_key(fp), {"until": until, "score": score}, ttl)
                        self._ban_index_upsert(fp, until)

                        cache.delete(self._ban_score_key(fp))
                        return self._deny("금칙어가 반복 감지되어 24시간 게시판 작성이 제한됩니다.")

                    return self._deny(f"{reason} (누적 {score}/{threshold})")

                return self._deny(reason)

        return self.get_response(request)

    def _is_board_path(self, path: str) -> bool:
        prefix = getattr(settings, "BOARD_PATH_PREFIX", "/board/")
        return path.startswith(prefix)

    def _deny(self, reason: str) -> HttpResponseForbidden:
        html = f"""<!doctype html><html lang="ko"><meta charset="utf-8">
<title>차단됨</title>
<style>
body{{font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans KR",sans-serif;margin:24px;color:#0f172a}}
.card{{max-width:760px;margin:auto;border:1px solid rgba(148,163,184,.35);border-radius:16px;padding:18px;background:#fff}}
h1{{font-size:18px;margin:0 0 10px}}
p{{margin:0;line-height:1.65;color:#334155}}
</style>
<div class="card">
  <h1>요청이 차단되었습니다</h1>
  <p>{reason}</p>
</div>
</html>"""
        return HttpResponseForbidden(html)

    def _inspect(self, request) -> Tuple[str, int]:
        title = (request.POST.get("title") or "").strip()
        body = (request.POST.get("body") or "").strip()
        guest_name = (request.POST.get("guest_name") or "").strip()
        text = "\n".join([title, body, guest_name]).strip()

        if not text:
            return ("", 0)

        # ✅ 링크 제한(레벨링): 외부 URL 포함 제출 시도 → 차단 + 자동 신고 생성
        fp = self._fingerprint(request)
        lb = cache.get(f"board:linkblock:{fp}")
        if lb and self._url_re.search(text):
            left = None
            try:
                if isinstance(lb, dict) and "until" in lb:
                    left = max(0, int(lb["until"]) - int(time.time()))
            except Exception:
                left = None

            urls = self._url_extract_re.findall(text)[:3]
            self._auto_report_link_violation(request, fp=fp, text=text, urls=urls, left=left)

            if isinstance(left, int) and left > 0:
                mins = max(1, left // 60)
                return (f"외부 링크(URL) 작성이 제한된 상태입니다. (남은 약 {mins}분)", 0)
            return ("외부 링크(URL) 작성이 제한된 상태입니다.", 0)

        if len(text) > 20000:
            return ("내용이 너무 깁니다.", 0)

        if len(self._url_re.findall(text)) >= 6:
            return ("링크가 너무 많습니다.", 0)

        if self._repeat_char_re.search(text):
            return ("같은 문자가 과도하게 반복되었습니다.", 0)

        if self._laugh_re.search(text):
            return ("도배로 판단되는 반복 문자가 감지되었습니다.", 0)

        if self._word_repeat_re.search(text):
            return ("같은 단어가 과도하게 반복되었습니다.", 0)

        # 중복 제출(5분)
        h = hashlib.sha1(f"{fp}|{title}|{body}".encode("utf-8")).hexdigest()
        if cache.get(f"board:dup:{h}"):
            return ("같은 내용이 반복 제출된 것으로 보입니다.", 0)
        cache.set(f"board:dup:{h}", 1, 300)

        banned = self._load_banned()
        low = text.lower()

        for w in banned.get("words", []):
            if w and w.lower() in low:
                return ("금칙어가 포함되어 있습니다.", self._ban_points_word())

        for rx in banned.get("regex", []):
            try:
                if re.search(rx, text, re.I):
                    return ("허용되지 않는 표현이 감지되었습니다.", self._ban_points_regex())
            except re.error:
                continue

        return ("", 0)

    # ─────────────────────────────────────────────────────────────
    # ✅ Auto report when link-blocked user tries to submit URLs
    # ─────────────────────────────────────────────────────────────

    def _guess_target_type_value(self, path: str) -> Any:
        """
        BoardReport.target_type 값 추정.
        - 모델 Enum이 있으면 그 값을 사용
        - 없으면 문자열로 fallback ('post' / 'comment')
        """
        is_comment = ("/comment" in (path or ""))
        # Enum이 존재하면 사용
        try:
            tt = getattr(BoardReport, "TargetType", None) if BoardReport is not None else None
            if tt is not None:
                if is_comment and hasattr(tt, "COMMENT"):
                    return tt.COMMENT
                if (not is_comment) and hasattr(tt, "POST"):
                    return tt.POST
        except Exception:
            pass
        return "comment" if is_comment else "post"

    def _guess_open_status_value(self) -> Any:
        """
        BoardReport.status 기본값이 open일 가능성이 높지만,
        Enum이 있으면 명시적으로 OPEN 사용.
        """
        try:
            st = getattr(BoardReport, "Status", None) if BoardReport is not None else None
            if st is not None and hasattr(st, "OPEN"):
                return st.OPEN
        except Exception:
            pass
        return "open"

    def _auto_report_link_violation(self, request, *, fp: str, text: str, urls: List[str], left: Optional[int]):
        if BoardReport is None:
            return

        # 너무 자주 쌓이지 않게 fp+내용 기반으로 10분 dedupe
        try:
            head = (text or "").strip().replace("\n", " ")[:160]
            sig = hashlib.sha1(f"{fp}|{'|'.join(urls)}|{head}".encode("utf-8")).hexdigest()[:12]
            dk = f"board:auto_report:url_block:{fp}:{sig}"
            if cache.get(dk):
                return
            cache.set(dk, 1, 600)
        except Exception:
            pass

        path = (getattr(request, "path_info", "") or "")[:200]
        u = getattr(request, "user", None)
        reporter = u if u and getattr(u, "is_authenticated", False) else None

        left_str = ""
        if isinstance(left, int):
            left_str = f"{max(0, left)}s_left"

        msg_lines = [
            "[AUTO] 링크 제한 상태에서 외부URL 포함 제출 시도",
            f"path={path}",
            f"fp={fp}",
        ]
        if left_str:
            msg_lines.append(left_str)
        if urls:
            msg_lines.append("urls=" + " | ".join(urls[:3]))
        msg_lines.append("preview=" + (head or "(empty)"))

        message = "\n".join(msg_lines)

        # create()는 필드 구성이 환경마다 달라서 2단계로 안전하게 시도
        base_kwargs = {
            "target_type": self._guess_target_type_value(path),
            "reason": "other",
            "message": message,
            "reporter_fp": fp,
            "status": self._guess_open_status_value(),
        }

        try:
            # reporter 필드가 있으면 붙이기
            if reporter is not None:
                try:
                    BoardReport.objects.create(**base_kwargs, reporter=reporter)  # type: ignore
                    return
                except TypeError:
                    pass
                except Exception:
                    # reporter 때문에 실패일 수 있으니 fallback
                    pass

            BoardReport.objects.create(**base_kwargs)  # type: ignore
        except Exception:
            # 모델/마이그레이션/필드 차이 등 어떤 이유로든 서비스가 죽으면 안 됨
            return

    # ───────── Ban helpers ─────────

    def _ban_score_key(self, fp: str) -> str:
        return f"board:ban_score:{fp}"

    def _ban_key(self, fp: str) -> str:
        return f"board:ban:{fp}"

    def _ban_index_key(self) -> str:
        return "board:ban_index:v1"

    def _ban_index_ttl(self) -> int:
        try:
            return int(getattr(settings, "BOARD_BAN_INDEX_TTL_SEC", 7 * 86400))
        except Exception:
            return 7 * 86400

    def _ban_threshold(self) -> int:
        try:
            return int(getattr(settings, "BOARD_BAN_THRESHOLD", 3))
        except Exception:
            return 3

    def _ban_ttl(self) -> int:
        try:
            return int(getattr(settings, "BOARD_BAN_TTL_SEC", 86400))
        except Exception:
            return 86400

    def _ban_points_word(self) -> int:
        try:
            return int(getattr(settings, "BOARD_BAN_POINTS_WORD", 1))
        except Exception:
            return 1

    def _ban_points_regex(self) -> int:
        try:
            return int(getattr(settings, "BOARD_BAN_POINTS_REGEX", 2))
        except Exception:
            return 2

    def _bump_ban_score(self, fp: str, points: int) -> Tuple[int, int]:
        key = self._ban_score_key(fp)
        threshold = self._ban_threshold()
        ttl = self._ban_ttl()

        val = cache.get(key)
        if val is None:
            cache.set(key, points, ttl)
            return (points, threshold)

        try:
            newv = cache.incr(key, points)  # type: ignore
        except Exception:
            newv = int(val) + int(points)
            cache.set(key, newv, ttl)

        return (int(newv), threshold)

    def _ban_seconds_left(self, ban_info: Any):
        try:
            if isinstance(ban_info, dict) and "until" in ban_info:
                left = int(ban_info["until"]) - int(time.time())
                return max(0, left)
        except Exception:
            pass
        return None

    def _ban_index_upsert(self, fp: str, until: int):
        key = self._ban_index_key()
        ttl = self._ban_index_ttl()

        try:
            lst = cache.get(key) or []
            if not isinstance(lst, list):
                lst = []
        except Exception:
            lst = []

        now = int(time.time())
        found = False
        for it in lst:
            if isinstance(it, dict) and it.get("fp") == fp:
                it["until"] = until
                it["updated"] = now
                found = True
                break
        if not found:
            lst.insert(0, {"fp": fp, "until": until, "updated": now})

        if len(lst) > 500:
            lst = lst[:500]

        try:
            cache.set(key, lst, ttl)
        except Exception:
            pass

    # ───────── misc ─────────

    def _fingerprint(self, request) -> str:
        ip = (
            request.META.get("HTTP_CF_CONNECTING_IP")
            or request.META.get("HTTP_X_FORWARDED_FOR")
            or request.META.get("REMOTE_ADDR")
            or ""
        )
        ip = (ip.split(",")[0] if ip else "").strip()
        ua = request.META.get("HTTP_USER_AGENT") or ""
        return hashlib.sha1(f"{ip}|{ua}".encode("utf-8")).hexdigest()[:12]

    def _load_banned(self) -> Dict[str, List[str]]:
        ck = "board:banned:v1"
        cached = cache.get(ck)
        if cached:
            return cached

        words = list(getattr(settings, "BOARD_BANNED_WORDS", []))
        regex = list(getattr(settings, "BOARD_BANNED_REGEX", []))

        if BoardAbuseKeyword is not None:
            try:
                for k in BoardAbuseKeyword.objects.filter(enabled=True):
                    if k.is_regex:
                        regex.append(k.pattern)
                    else:
                        words.append(k.pattern)
            except Exception:
                pass

        out = {"words": [x for x in words if x], "regex": [x for x in regex if x]}
        cache.set(ck, out, 60)
        return out
