# ragapp/admin.py
from __future__ import annotations
import csv, hashlib, mimetypes, logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from django.contrib import admin, messages
from django.db.models import Max
from django.utils import timezone
from django.urls import reverse, path
from django.http import HttpRequest, HttpResponse
from django.middleware.csrf import get_token
from django.conf import settings
from ragapp.models import TableSearchRule
from django.utils.html import mark_safe, format_html
from django.contrib.admin.sites import AlreadyRegistered

from ragapp.models import (
    MyLog,
    RagSetting,
    FaqEntry,
    ChatQueryLog,
    Feedback,
    IngestHistory,
    LegalConfig,
    RagChunk,
    LiveChatSession,
    TableSchema,   # ✅ 표 스키마
    QaragFeedback,
    WebFeedback,
    RagSearchFeedback,
    FeedbackLog,
    FeedbackReview,
    AppLog,
    LiveChatMessage,  # ✅ 세션 로그용
)

# ✅ RAG 전용 AdminSite 인스턴스
from ragapp.admin_site import rag_admin_site

# 선택 모델 (있을 수도 있고, 없을 수도 있음)
try:
    from ragapp.models import MediaAsset, TableDataset  # type: ignore
    _HAS_MEDIA_MODELS = True
except Exception:
    _HAS_MEDIA_MODELS = False

# --- 여기부터 추가 ---
# 채팅 보존/욕설 감지 관련 admin 등록 붙이기
try:
    from . import admin_chat_retention  # noqa: F401
except Exception as e:  # import 에러 나면 서버가 죽지 않게 로그만 남김
    import logging
    logging.getLogger(__name__).warning("admin_chat_retention import failed: %s", e)


def _safe_register(site, model, admin_class=None):
    try:
        if admin_class is None:
            site.register(model)
        else:
            site.register(model, admin_class)
    except AlreadyRegistered:
        pass


log = logging.getLogger(__name__)

# ─────────────────────────────
# MyLog
# ─────────────────────────────
class MyLogAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "mode_text", "query", "ok_flag", "remote_addr_text")
    list_filter = ("mode_text", "ok_flag")
    search_fields = ("query", "remote_addr_text", "extra_json")
    readonly_fields = ("created_at", "mode_text", "query", "ok_flag", "remote_addr_text", "extra_json")
    fieldsets = (
        (None, {"fields": ("created_at", "mode_text", "query", "ok_flag", "remote_addr_text")}),
        ("추가 정보(JSON})", {"fields": ("extra_json",)}),
    )


# ─────────────────────────────
# RagSetting
# ─────────────────────────────
class RagSettingAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "chroma_db_dir",
        "chroma_collection",
        "news_topk",
        "rag_query_topk",
        "rag_fallback_topk",
        "rag_max_sources",
        "auto_ingest_after_gemini",
        "web_ingest_to_chroma",
        "crawl_answer_links",
        "action_links",
    )

    def action_links(self, obj):
        def pill(href: str, label: str, style: str) -> str:
            return f'<a href="{href}" style="{style}">{label}</a>'

        base = (
            "display:inline-block;margin:0 4px 4px 0;padding:4px 8px;border-radius:6px;"
            "font-size:11px;font-weight:500;line-height:1.2;text-decoration:none;border:1px solid transparent;"
            "box-shadow:0 1px 2px rgba(0,0,0,.08);"
        )
        styles = {
            "edit": base + "background:linear-gradient(90deg,#6366f1,#4f46e5);color:#fff;border-color:rgba(99,102,241,.6);",
            "delete": base + "background:linear-gradient(90deg,#ef4444,#dc2626);color:#fff;border-color:rgba(239,68,68,.5);",
        }

        # ✅ 'admin' 또는 'ragadmin' 중, 지금 들어온 곳만 사용
        ns = self.admin_site.name

        change_url = reverse(f"{ns}:ragapp_ragsetting_change", args=[obj.pk])
        delete_url = reverse(f"{ns}:ragapp_ragsetting_delete", args=[obj.pk])

        return mark_safe(
            pill(change_url, "✏️ 수정", styles["edit"]) +
            pill(delete_url, "🗑 삭제", styles["delete"])
        )

    action_links.short_description = "관리 액션"


# ─────────────────────────────
# ChatQueryLog
# ─────────────────────────────
class ChatQueryLogAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "created_at",
        "mode_badge",
        "short_q",
        "short_a",
        "is_error",
        "was_helpful",
        "feedback_short",
        "client_ip",
    )
    list_display_links = ("short_q",)
    list_filter = ("mode", "was_helpful", "is_error", "created_at")
    search_fields = ("question", "answer_excerpt", "client_ip", "feedback")
    ordering = ("-created_at", "-id")
    date_hierarchy = "created_at"
    list_per_page = 50
    empty_value_display = "-"
    actions = ("mark_helpful", "mark_unhelpful", "clear_feedback")
    readonly_fields = (
        "created_at",
        "mode",
        "question",
        "answer_excerpt",
        "client_ip",
        "is_error",
        "error_msg",
        "was_helpful",
        "feedback",
        "legal_basis",
        "consent_version",
        "consent_log",
        "legal_hold",
        "delete_at",
    )

    def mark_helpful(self, request, qs):
        c = qs.update(was_helpful=True)
        self.message_user(request, f"{c}개를 Helpful로 표시했습니다.")

    mark_helpful.short_description = "선택 항목 Helpful로 표시"

    def mark_unhelpful(self, request, qs):
        c = qs.update(was_helpful=False)
        self.message_user(request, f"{c}개를 Not helpful로 표시했습니다.")

    mark_unhelpful.short_description = "선택 항목 Not helpful로 표시"

    def clear_feedback(self, request, qs):
        c = qs.update(feedback="")
        self.message_user(request, f"{c}개의 코멘트를 비웠습니다.")

    clear_feedback.short_description = "선택 항목 코멘트 비우기"

    def mode_badge(self, obj):
        from django.utils.html import format_html

        color = {
            "rag": "#38bdf8",
            "gemini": "#a78bfa",
            "faq": "#10b981",
            "blocked": "#f87171",
        }.get(obj.mode, "#94a3b8")
        return format_html(
            '<span style="padding:2px 8px;border-radius:999px;background:{}20;color:{}">{}</span>',
            color,
            color,
            (obj.mode or "").upper(),
        )

    mode_badge.short_description = "Mode"

    def feedback_short(self, obj):
        txt = (obj.feedback or "").strip().replace("\n", " ")
        return (txt[:40] + "...") if len(txt) > 40 else txt

    feedback_short.short_description = "피드백 코멘트"

    def short_q(self, obj):
        q = (obj.question or "").strip().replace("\n", " ")
        return (q[:30] + "...") if len(q) > 30 else q

    short_q.short_description = "질문 미리보기"

    def short_a(self, obj):
        a = (obj.answer_excerpt or "").strip().replace("\n", " ")
        return (a[:30] + "...") if len(a) > 30 else a

    short_a.short_description = "답변 미리보기"


# ─────────────────────────────
# FaqEntry
# ─────────────────────────────
class FaqEntryAdmin(admin.ModelAdmin):
    list_display = ("short_question", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("question", "answer")
    ordering = ("-updated_at",)
    readonly_fields = ("created_at", "updated_at")

    def short_question(self, obj):
        q = (obj.question or "").strip().replace("\n", " ")
        return (q[:60] + "…") if len(q) > 60 else q

    short_question.short_description = "질문"


# ─────────────────────────────
# Feedback
# ─────────────────────────────
class FeedbackAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "answer_type", "is_helpful", "short_question", "short_answer")
    list_filter = ("answer_type", "is_helpful", "created_at")
    search_fields = ("question", "answer")
    readonly_fields = ("created_at", "question", "answer", "answer_type", "is_helpful", "sources_json")

    def short_question(self, obj):
        txt = (obj.question or "").strip().replace("\n", " ")
        return (txt[:60] + "...") if len(txt) > 60 else txt

    short_question.short_description = "Question"

    def short_answer(self, obj):
        txt = (obj.answer or "").strip().replace("\n", " ")
        return (txt[:60] + "...") if len(txt) > 60 else txt

    short_answer.short_description = "Answer Preview"

    def short_q(self):
        txt = (self.question or "").strip().replace("\n", " ")
        return txt[:50] + ("..." if len(txt) > 50 else "")

    short_q.short_description = "질문 미리보기"

    def short_a(self):
        txt = (self.answer or "").strip().replace("\n", " ")
        return txt[:50] + ("..." if len(txt) > 50 else "")

    short_a.short_description = "답변 미리보기"


# ─────────────────────────────
# IngestHistory
# ─────────────────────────────
class IngestHistoryAdmin(admin.ModelAdmin):
    list_display = ("created_at", "keyword", "ingested_count", "total_candidates", "skipped_count", "failed_count")
    list_filter = ("keyword", "created_at")
    search_fields = ("keyword",)
    readonly_fields = (
        "created_at",
        "keyword",
        "total_candidates",
        "ingested_count",
        "skipped_count",
        "failed_count",
        "detail",
    )


# ─────────────────────────────
# LegalConfig
# ─────────────────────────────
class LegalConfigAdmin(admin.ModelAdmin):
    search_fields = ("service_name", "operator_name", "contact_email")


# ─────────────────────────────
# RagChunk
# ─────────────────────────────
class RagChunkAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "doc_id", "dim", "created_at")
    list_filter = ("dim", "created_at")
    search_fields = ("title", "url", "doc_id", "text")
    readonly_fields = ("created_at",)
    ordering = ("-created_at",)


try:
    rag_admin_site.register(RagChunk, RagChunkAdmin)
except AlreadyRegistered:
    pass


# ─────────────────────────────
# LiveChatSession
# ─────────────────────────────
def _model_fieldnames(model) -> set[str]:
    out: set[str] = set()
    try:
        for f in model._meta.get_fields():
            name = getattr(f, "name", None)
            if name:
                out.add(name)
    except Exception:
        pass
    return out


_LCS_FIELDS = _model_fieldnames(LiveChatSession)


def _has(name: str) -> bool:
    return name in _LCS_FIELDS


def _pick(*names: str) -> list[str]:
    return [n for n in names if _has(n)]


@admin.register(LiveChatSession)
class LiveChatSessionAdmin(admin.ModelAdmin):
    """
    LiveChatSession 목록 + 세션별 전체 대화 로그 보기 링크.
    """

    # --- list_display: (존재하는 필드) + (admin 메서드들)
    _ld: list[str] = []
    _ld += _pick("id", "code", "room")
    _ld += ["status_badge", "source", "user_name", "client_ip", "connected_at", "last_message_at"]
    _ld += _pick("created_at", "started_at", "ended_at", "processed_at")
    _ld += ["view_log_link"]  # ✅ 마지막에 '대화 보기' 링크
    list_display = tuple(_ld)

    # --- readonly_fields: 존재하는 필드 + admin 메서드
    _ro: list[str] = []
    _ro += _pick(
        "id",
        "code",
        "room",
        "status",
        "created_at",
        "started_at",
        "ended_at",
        "processed_at",
        "purge_at",
        "session_type",
        "session_note",
        "session_detail",
        "memo",
        "page_title",
        "page_path",
    )
    _ro += ["connected_at", "last_message_at", "view_log_link"]  # ✅ 메서드도 readonly 로
    readonly_fields = tuple(_ro)

    # --- list_filter: 무조건 "모델 Field"만 가능
    list_filter = tuple(_pick("status", "created_at"))

    # --- search_fields도 존재하는 필드만
    search_fields = tuple(_pick("room", "code", "session_note", "memo", "page_title", "page_path"))

    # --- 정렬
    if _has("created_at"):
        ordering = ("-created_at", "-id")
        date_hierarchy = "created_at"
    else:
        ordering = ("-id",)

    # ─────────────────────────────────────────
    #  Admin URL 확장: /admin/ragapp/livechatsession/<id>/log/
    # ─────────────────────────────────────────
    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:session_id>/log/",
                self.admin_site.admin_view(self.session_log_view),
                name="ragapp_livechatsession_log",
            ),
        ]
        return custom + urls

    # ✅ 리스트에서 보이는 "대화 보기" 링크
    @admin.display(description="대화 로그")
    def view_log_link(self, obj: LiveChatSession):
        try:
            url = reverse(f"{self.admin_site.name}:ragapp_livechatsession_log", args=[obj.pk])
        except Exception:
            # 혹시 reverse 실패하면 대략적인 기본 경로
            url = f"/admin/ragapp/livechatsession/{obj.pk}/log/"
        return format_html('<a href="{}" target="_blank">대화 보기</a>', url)

    # ─────────────────────────────────────────
    # ✅ admin 표시용 메서드들
    # ─────────────────────────────────────────
    @admin.display(description="상태")
    def status_badge(self, obj: LiveChatSession):
        """
        LiveChatSession.Status:
        - waiting / active / ended_need_save / saved / deleted(+ 과거 ended/done 호환)
        """
        raw = getattr(obj, "status", "") or ""
        v = str(raw).lower()

        # Choice 라벨이 있으면 그걸 우선 사용
        label = raw
        try:
            if hasattr(obj, "get_status_display"):
                label = obj.get_status_display() or raw
        except Exception:
            pass

        color = "#6b7280"  # 기본 회색
        if v in ("waiting", "대기"):
            color = "#38bdf8"  # 파랑
        elif v in ("active", "진행", "running"):
            color = "#22c55e"  # 초록
        elif v in ("ended_need_save", "need_save", "ended", "종료", "done"):
            color = "#f97316"  # 주황: 상담 종료, 기록 필요
        elif v in ("saved", "완료"):
            color = "#6366f1"  # 남색: 기록 완료
        elif v in ("deleted", "closed"):
            color = "#9ca3af"  # 연회색

        label = label or raw or "-"
        return format_html(
            '<span style="padding:2px 8px;border-radius:999px;'
            "background:{}20;color:{};font-size:11px;font-weight:600;\">{}</span>",
            color,
            color,
            label,
        )

    @admin.display(description="유입(source)")
    def source(self, obj: LiveChatSession):
        v = getattr(obj, "source", None)
        return v if v not in (None, "") else "-"

    @admin.display(description="사용자명")
    def user_name(self, obj: LiveChatSession):
        for key in ("user_name", "client_name", "name"):
            v = getattr(obj, key, None)
            if v not in (None, ""):
                return v
        return "-"

    @admin.display(description="클라이언트 IP")
    def client_ip(self, obj: LiveChatSession):
        v = getattr(obj, "client_ip", None)
        return v if v not in (None, "") else "-"

    @admin.display(description="연결 시각")
    def connected_at(self, obj: LiveChatSession):
        for key in ("connected_at", "started_at", "created_at"):
            v = getattr(obj, key, None)
            if v:
                try:
                    return timezone.localtime(v)
                except Exception:
                    return v
        return "-"

    @admin.display(description="마지막 메시지 시각")
    def last_message_at(self, obj: LiveChatSession):
        # 1) annotate 값 우선
        v = getattr(obj, "_last_message_at", None)
        if v:
            try:
                return timezone.localtime(v)
            except Exception:
                return v

        # 2) messages 관계가 있으면 거기서 조회
        try:
            rel = getattr(obj, "messages", None)
            if rel is not None:
                m = rel.order_by("-created_at").first()
                t = getattr(m, "created_at", None) if m else None
                if t:
                    try:
                        return timezone.localtime(t)
                    except Exception:
                        return t
        except Exception:
            pass

        # 3) fallback
        for key in ("ended_at", "processed_at", "started_at", "created_at"):
            t = getattr(obj, key, None)
            if t:
                try:
                    return timezone.localtime(t)
                except Exception:
                    return t
        return "-"

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # messages 관계가 있을 때만 annotate (N+1 완화)
        try:
            if hasattr(LiveChatSession, "messages"):
                qs = qs.annotate(_last_message_at=Max("messages__created_at"))
        except Exception:
            pass
        return qs

    # ─────────────────────────────────────────
    #  세션 전체 로그 뷰 (새 탭)
    # ─────────────────────────────────────────
    def session_log_view(self, request: HttpRequest, session_id: int, *args, **kwargs):
        """
        /admin/.../livechatsession/<id>/log/ 에서 전체 대화 로그를 한 번에 보여주는 뷰
        """
        try:
            session = LiveChatSession.objects.get(pk=session_id)
        except LiveChatSession.DoesNotExist:
            return HttpResponse("세션을 찾을 수 없습니다.", status=404)

        # 해당 세션의 LiveChatMessage 전부
        msgs = (
            LiveChatMessage.objects.filter(session=session)
            .order_by("created_at")
        )

        rows: list[str] = []
        from django.utils.html import escape

        for m in msgs:
            ts = getattr(m, "created_at", None)
            if ts:
                try:
                    ts_str = timezone.localtime(ts).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    ts_str = str(ts)
            else:
                ts_str = ""

            role = getattr(m, "role", getattr(m, "sender", "")) or ""
            content = getattr(m, "content", getattr(m, "text", "")) or ""

            rows.append(
                "<tr>"
                f"<td class='ts'>{escape(ts_str)}</td>"
                f"<td class='role'>{escape(role)}</td>"
                f"<td class='msg'>{escape(content)}</td>"
                "</tr>"
            )

        tbody = "".join(rows) or "<tr><td colspan='3'>메시지가 없습니다.</td></tr>"

        title = f"Live chat session #{session.id} (room={session.room})"

        html = f"""<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8">
<title>{escape(title)}</title>
<style>
 body{{font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans KR",sans-serif;margin:0;padding:16px;background:#0f172a;color:#e5e7eb}}
 .wrap{{max-width:960px;margin:0 auto;background:#020617;border-radius:12px;padding:20px;border:1px solid #1f2937}}
 h1{{font-size:18px;margin-top:0;margin-bottom:12px}}
 table{{width:100%;border-collapse:collapse;font-size:13px}}
 th,td{{border-bottom:1px solid #1f2937;padding:6px 8px;vertical-align:top}}
 th{{text-align:left;background:#020617;color:#9ca3af;font-weight:600}}
 tr:nth-child(even) td{{background:#020617}}
 .ts{{width:150px;white-space:nowrap;color:#9ca3af}}
 .role{{width:80px;font-weight:600}}
 .msg{{white-space:pre-wrap}}
 .meta{{font-size:12px;color:#9ca3af;margin-bottom:8px}}
 a{{color:#93c5fd;text-decoration:none}}
 a:hover{{text-decoration:underline}}
</style></head>
<body>
<div class="wrap">
<h1>{escape(title)}</h1>
<div class="meta">
 room = {escape(session.room or "")}
 · status = {escape(str(getattr(session,"status","") or ""))}
 · created_at = {escape(str(getattr(session,"created_at","") or ""))}
 · <a href="{escape(reverse(f"{self.admin_site.name}:ragapp_livechatsession_change", args=[session.id]))}">세션으로 돌아가기</a>
</div>
<table>
<thead><tr><th>시간</th><th>역할</th><th>내용</th></tr></thead>
<tbody>
{tbody}
</tbody>
</table>
</div>
</body></html>"""
        return HttpResponse(html)


# ─────────────────────────────
# TableSchema
# ─────────────────────────────
class TableSchemaAdmin(admin.ModelAdmin):
    """
    CSV/엑셀로 올린 표 구조 확인용 Admin
    - 어떤 컬럼이 있고, 샘플 데이터가 어떻게 생겼는지 한눈에 보기
    """
    list_display = ("table_name", "created_at", "updated_at")
    search_fields = ("table_name",)
    readonly_fields = ("columns", "column_types", "sample_rows", "created_at", "updated_at")
    ordering = ("table_name",)


# ─────────────────────────────
# Log 분석
# ─────────────────────────────
@admin.register(AppLog)
class AppLogAdmin(admin.ModelAdmin):
    """
    공통 애플리케이션 로그
    - meta(JSON)에 들어간 view / stage / result 등을 컬럼으로 꺼내서 보여준다.
    """

    list_display = (
        "created_at",
        "level",
        "component",
        "trace_id",
        "op_name",
        "stage",
        "result_badge",
        "short_message",
    )
    # ✅ 실제 필드만 사용 (logger_name, extra_json 제거)
    list_filter = ("level", "component", "created_at")
    search_fields = ("message", "component", "trace_id", "meta")
    date_hierarchy = "created_at"
    readonly_fields = (
        "created_at",
        "level",
        "component",
        "trace_id",
        "message",
        "meta",
    )
    ordering = ("-created_at",)

    # 내부 헬퍼: meta(JSON) 안전하게 꺼내기
    def _extra(self, obj):
        # AppLog.meta 가 없으면 {} 리턴 (방어코드)
        return getattr(obj, "meta", None) or {}

    def op_name(self, obj):
        """
        어디에서 찍힌 로그인지
        - meta.view > meta.op > component 순으로 사용
        """
        extra = self._extra(obj)
        return extra.get("view") or extra.get("op") or obj.component or "-"

    op_name.short_description = "어디서"

    def stage(self, obj):
        """
        단계 (start / done / no_text / error ... )
        """
        extra = self._extra(obj)
        return extra.get("stage") or "-"

    stage.short_description = "단계"

    def result_badge(self, obj):
        """
        성공/실패 여부를 한 눈에 보기 위한 표시
        - meta.result 를 우선 사용
        - 없으면 level 기반으로 추론
        """
        from django.utils.html import format_html

        extra = self._extra(obj)
        r = (extra.get("result") or "").lower()

        # result 없으면 level로 추론
        if not r:
            if obj.level in ("ERROR", "CRITICAL"):
                r = "fail"
            elif obj.level == "WARNING":
                r = "warn"
            elif obj.level in ("INFO", "DEBUG"):
                r = "success"

        if r in ("success", "ok"):
            return format_html('<span style="color:#16a34a;font-weight:600;">성공</span>')
        if r in ("fail", "error"):
            return format_html('<span style="color:#dc2626;font-weight:600;">실패</span>')
        if r == "warn":
            return format_html('<span style="color:#ea580c;font-weight:600;">주의</span>')
        return "-"

    result_badge.short_description = "결과"

    def short_message(self, obj):
        text = obj.message or ""
        return text if len(text) <= 80 else text[:77] + "..."

    short_message.short_description = "메시지 요약"
    short_message.admin_order_field = "message"


# ─────────────────────────────
# 기본 admin.site 등록
# ─────────────────────────────
admin.site.register(MyLog, MyLogAdmin)
admin.site.register(RagSetting, RagSettingAdmin)
admin.site.register(ChatQueryLog, ChatQueryLogAdmin)
admin.site.register(FaqEntry, FaqEntryAdmin)
admin.site.register(Feedback, FeedbackAdmin)
admin.site.register(IngestHistory, IngestHistoryAdmin)
admin.site.register(LegalConfig, LegalConfigAdmin)
admin.site.register(TableSchema, TableSchemaAdmin)   # ✅ 표 스키마
_safe_register(admin.site, LiveChatSession, LiveChatSessionAdmin)
_safe_register(rag_admin_site, LiveChatSession, LiveChatSessionAdmin)
_safe_register(admin.site, LiveChatSession, LiveChatSessionAdmin)

# ─────────────────────────────
# rag_admin_site 등록
# ─────────────────────────────
rag_admin_site.register(MyLog, MyLogAdmin)
rag_admin_site.register(RagSetting, RagSettingAdmin)
rag_admin_site.register(ChatQueryLog, ChatQueryLogAdmin)
rag_admin_site.register(FaqEntry, FaqEntryAdmin)
rag_admin_site.register(Feedback, FeedbackAdmin)
rag_admin_site.register(IngestHistory, IngestHistoryAdmin)
rag_admin_site.register(LegalConfig, LegalConfigAdmin)
rag_admin_site.register(TableSchema, TableSchemaAdmin)   # ✅ 표 스키마
# ✅ RagChunk는 둘 다(기본 admin / rag_admin_site)에서 보이게 하고 싶으면
_safe_register(admin.site, RagChunk, RagChunkAdmin)
_safe_register(rag_admin_site, RagChunk, RagChunkAdmin)
_safe_register(rag_admin_site, AppLog, AppLogAdmin)
_safe_register(rag_admin_site, LiveChatSession, LiveChatSessionAdmin)

# ─────────────────────────────
# 선택: MediaAsset / TableDataset
# ─────────────────────────────
if _HAS_MEDIA_MODELS:

    class MediaAssetAdmin(admin.ModelAdmin):
        list_display = ("id", "file", "caption", "indexed_at", "size", "mime")
        search_fields = ("caption", "file")

        def get_urls(self):
            urls = super().get_urls()
            custom = [
                path(
                    "search/",
                    self.admin_site.admin_view(self.search_view),
                    name="mediaasset_search",
                )
            ]
            return custom + urls

        def search_view(self, request: HttpRequest):
            token = get_token(request)
            html_top = f"""
            <div class="ma-wrap"><div class="ma-card">
              <div class="ma-h1">Media 이미지 검색</div>
              <form method="post" class="ma-form" style="margin-bottom:12px">
                <input type="hidden" name="csrfmiddlewaretoken" value="{token}">
                <input name="q" type="text" placeholder="예: 노을 바다 풍경" style="width:420px" required>
                <input name="k" type="number" value="8" min="1" max="50" style="width:80px">
                <button class="ma-btn" type="submit">검색</button>
                <a class="ma-link" href="{reverse('ragadmin:ragapp_mediaasset_changelist')}" style="margin-left:8px">← 목록으로</a>
              </form>
            """
            if request.method != "POST":
                return HttpResponse(html_top + "</div></div>")

            from ragapp.services.vertex_embed import embed_text_mm
            from ragapp.services.chroma_media import search_images_by_text_embedding

            q = (request.POST.get("q") or "").strip()
            try:
                k = int(request.POST.get("k") or 8)
            except Exception:
                k = 8

            try:
                qv = embed_text_mm(q)
                res = search_images_by_text_embedding(text_embedding=qv, k=k) or {}
                ids = (res.get("ids") or [[]])[0] if isinstance(res.get("ids"), list) else []
                metas = (res.get("metadatas") or [[]])[0] if isinstance(res.get("metadatas"), list) else []
                docs = (res.get("documents") or [[]])[0] if isinstance(res.get("documents"), list) else []

                rows = []
                for i, (pid, meta, doc) in enumerate(zip(ids, metas, docs), 1):
                    path_val = (meta or {}).get("path", "")
                    rows.append(
                        f"<tr><td>{i}</td><td>{pid}</td><td>{doc or '-'}</td>"
                        f"<td class='mono'>{path_val or '-'}</td></tr>"
                    )

                table = f"""
                  <table class="ma-table">
                    <thead><tr><th>#</th><th>ID</th><th>캡션</th><th>파일경로</th></tr></thead>
                    <tbody>{''.join(rows) or "<tr><td colspan='4'>결과 없음</td></tr>"}</tbody>
                  </table>
                </div></div>
                """
                return HttpResponse(html_top + table)
            except Exception as e:
                return HttpResponse(
                    html_top + f"<p style='color:#fca5a5'>오류: {e}</p></div></div>"
                )

    class TableDatasetAdmin(admin.ModelAdmin):
        list_display = ("id", "table_name", "csv", "row_count", "indexed_at")
        search_fields = ("table_name", "csv")

    # 기본 admin + rag_admin_site 모두 등록
    admin.site.register(MediaAsset, MediaAssetAdmin)
    admin.site.register(TableDataset, TableDatasetAdmin)

    rag_admin_site.register(MediaAsset, MediaAssetAdmin)
    rag_admin_site.register(TableDataset, TableDatasetAdmin)


# ─────────────────────────────
# TableSearchRule (표 검색 규칙 하드코딩 대체)
# ─────────────────────────────
@admin.register(TableSearchRule)
class TableSearchRuleAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "table_name",
        "is_active",
        "min_sim",
        "hard_filter_enabled",
        "updated_at",
    )
    list_filter = ("is_active", "hard_filter_enabled")
    search_fields = ("name", "table_name")
    ordering = ("-updated_at", "-id")

    readonly_fields = ("created_at", "updated_at")

    fieldsets = (
        (
            "기본 정보",
            {
                "fields": (
                    "name",
                    "table_name",
                    "is_active",
                )
            },
        ),
        (
            "검색 동작 설정",
            {
                "fields": (
                    "min_sim",
                    "hard_filter_enabled",
                )
            },
        ),
        (
            "집계/컬럼 규칙(JSON)",
            {
                "description": (
                    "JSON 형식으로 입력하세요.<br>"
                    '예시 1) agg_hints_json: {"sum": ["합계","총액"], "avg": ["평균"]}<br>'
                    '예시 2) column_synonyms_json: {"region":["지역","지점"],"sales":["매출","금액"]}<br>'
                    '예시 3) numeric_hints_json: ["sales","amount","price"]'
                ),
                "fields": (
                    "agg_hints_json",
                    "column_synonyms_json",
                    "numeric_hints_json",
                ),
            },
        ),
        (
            "시스템 정보",
            {
                "classes": ("collapse",),
                "fields": ("created_at", "updated_at"),
            },
        ),
    )


# ✅ rag_admin_site 에도 노출 (관리자 콘솔에서 바로 접근)
_safe_register(rag_admin_site, TableSearchRule, TableSearchRuleAdmin)


@admin.register(QaragFeedback)
class QaragFeedbackAdmin(admin.ModelAdmin):
    """
    질문 챗봇(QARAG) 전용 피드백 관리자 화면.
    - 한 줄에: id / 생성시각 / 👍👎 / 질문 요약 / 세션 ID 정도만 보여준다.
    """

    list_display = (
        "id",
        "created_at",
        "is_helpful",
        "short_question",
        "session_id",
    )
    list_filter = (
        "is_helpful",
        "legal_basis",
        "created_at",
    )
    search_fields = (
        "question",
        "answer",
        "session_id",
        "client_ip",
    )

    # 모델에 실제로 있는 필드만 readonly로 둔다.
    readonly_fields = (
        "created_at",
        "delete_at",
    )

    def short_question(self, obj):
        txt = (obj.question or "").strip().replace("\n", " ")
        if len(txt) > 50:
            return txt[:50] + "..."
        return txt

    short_question.short_description = "질문 미리보기"


# ─────────────────────────────────────
# RAG 검색 피드백 Admin
# ─────────────────────────────────────
@admin.register(RagSearchFeedback)
class RagSearchFeedbackAdmin(admin.ModelAdmin):
    list_display = ("id", "short_q", "short_a", "is_helpful", "answer_type", "created_at")
    list_filter = ("is_helpful", "answer_type", "created_at")
    search_fields = ("question", "answer")
    readonly_fields = ("created_at",)

    def short_q(self, obj):
        q = (obj.question or "").strip()
        return q if len(q) <= 40 else q[:37] + "…"

    short_q.short_description = "질문 요약"

    def short_a(self, obj):
        a = (obj.answer or "").strip()
        return a if len(a) <= 40 else a[:37] + "…"

    short_a.short_description = "답변 요약"


# ─────────────────────────────────────
# 웹 검색 피드백 Admin
# ─────────────────────────────────────
@admin.register(WebFeedback)
class WebFeedbackAdmin(admin.ModelAdmin):
    list_display = ("id", "short_q", "short_a", "is_helpful", "created_at")
    list_filter = ("is_helpful", "created_at")
    search_fields = ("question", "answer")
    readonly_fields = ("created_at",)

    def short_q(self, obj):
        q = (obj.question or "").strip()
        return q if len(q) <= 40 else q[:37] + "…"

    short_q.short_description = "질문 요약"

    def short_a(self, obj):
        a = (obj.answer or "").strip()
        return a if len(a) <= 40 else a[:37] + "…"

    short_a.short_description = "답변 요약"


@admin.register(FeedbackLog)
class FeedbackLogAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "created_at",
        "answer_type",
        "channel_label",     # 웹 / RAG / 질문챗봇
        "helpful_icon",
        "short_question",
    )
    list_filter = ("answer_type", "helpful", "from_ui", "stage")
    search_fields = ("question", "answer", "comment")

    def helpful_icon(self, obj):
        if obj.helpful is True:
            return "👍"
        if obj.helpful is False:
            return "👎"
        return "-"

    helpful_icon.short_description = "평가"


@admin.register(FeedbackReview)
class FeedbackReviewAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "status",
        "owner",
        "feedback_channel",
        "feedback_short_q",
        "created_at",
        "updated_at",
    )
    list_filter = ("status", "owner")
    search_fields = ("plan", "resolution", "feedback__question", "feedback__answer")

    def feedback_channel(self, obj):
        # 웹 / RAG / 질문챗봇 표시
        return obj.feedback.get_answer_type_display()

    feedback_channel.short_description = "채널"

    def feedback_short_q(self, obj):
        return obj.feedback.short_question()

    feedback_short_q.short_description = "질문"
