# ragapp/admin.py
from __future__ import annotations

import logging
import hashlib
import mimetypes
from typing import Any

from django.conf import settings
from django.contrib import admin
from django.contrib.admin.sites import AlreadyRegistered, NotRegistered
from django.db.models import Max
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, mark_safe

from ragapp.models import (
    AppLog,
    ChatQueryLog,
    Feedback,
    FeedbackLog,
    FeedbackReview,
    FaqEntry,
    IngestHistory,
    LegalConfig,
    LiveChatSession,
    MyLog,
    QaragFeedback,
    RagChunk,
    RagSearchFeedback,
    RagSetting,
    TableSchema,
    TableSearchRule,
    WebFeedback,
)

log = logging.getLogger(__name__)

# ✅ RAG 전용 AdminSite 인스턴스 (순환 import 방지용: 실패해도 서버 죽지 않게)
try:
    from ragapp.admin_site import rag_admin_site  # type: ignore
except Exception as e:  # pragma: no cover
    rag_admin_site = None  # type: ignore
    log.warning("rag_admin_site import failed (circular import?). admin.site only. err=%s", e)

# 선택 모델 (있을 수도 있고, 없을 수도 있음)
try:
    from ragapp.models import MediaAsset, TableDataset  # type: ignore

    _HAS_MEDIA_MODELS = True
except Exception:
    _HAS_MEDIA_MODELS = False

# --- 채팅 보존/욕설 감지 관련 admin 등록 붙이기 (있으면 로드) ---
try:
    from . import admin_chat_retention  # noqa: F401
except Exception as e:  # pragma: no cover
    log.warning("admin_chat_retention import failed: %s", e)


def _safe_register(site: admin.sites.AdminSite, model, admin_class=None, *, force: bool = False) -> None:
    """
    force=True면: 기존 등록이 있으면 unregister 후 원하는 admin_class로 재등록
    force=False면: 이미 등록돼 있으면 그냥 패스
    """
    if site is None:
        return

    if force:
        try:
            site.unregister(model)
        except NotRegistered:
            pass

    try:
        if admin_class is None:
            site.register(model)
        else:
            site.register(model, admin_class)
    except AlreadyRegistered:
        pass


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

def _get_livechat_message_model():
    """
    ✅ 단일 출처 우선순위:
    1) models_chat_retention.LiveChatMessage
    2) models.LiveChatMessage (레거시)
    """
    try:
        from ragapp.models_chat_retention import LiveChatMessage as M  # type: ignore
        return M
    except Exception:
        pass
    try:
        from ragapp.models import LiveChatMessage as M  # type: ignore
        return M
    except Exception:
        return None


def _msg_text(m) -> str:
    return (
        getattr(m, "content", None)
        or getattr(m, "text", None)
        or getattr(m, "body", None)
        or getattr(m, "message", None)
        or ""
    )


def _msg_role(m) -> str:
    return getattr(m, "role", None) or getattr(m, "sender", None) or "system"


# ─────────────────────────────
# MyLog
# ─────────────────────────────
class MyLogAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "mode_text", "query", "ok_flag", "remote_addr_text")
    list_filter = ("mode_text", "ok_flag")
    search_fields = ("query", "remote_addr_text")
    readonly_fields = ("created_at", "mode_text", "query", "ok_flag", "remote_addr_text", "extra_json")
    fieldsets = (
        (None, {"fields": ("created_at", "mode_text", "query", "ok_flag", "remote_addr_text")}),
        ("추가 정보(JSON)", {"fields": ("extra_json",)}),
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
            "edit": base
            + "background:linear-gradient(90deg,#6366f1,#4f46e5);color:#fff;border-color:rgba(99,102,241,.6);",
            "delete": base
            + "background:linear-gradient(90deg,#ef4444,#dc2626);color:#fff;border-color:rgba(239,68,68,.5);",
        }

        ns = self.admin_site.name
        change_url = reverse(f"{ns}:ragapp_ragsetting_change", args=[obj.pk])
        delete_url = reverse(f"{ns}:ragapp_ragsetting_delete", args=[obj.pk])

        return mark_safe(pill(change_url, "✏️ 수정", styles["edit"]) + pill(delete_url, "🗑 삭제", styles["delete"]))

    action_links.short_description = "관리 액션"


# ─────────────────────────────
# ChatQueryLog
# ─────────────────────────────
class ChatQueryLogAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "mode_badge", "short_q", "short_a", "is_error", "was_helpful", "feedback_short", "client_ip")
    list_display_links = ("short_q",)
    list_filter = ("mode", "was_helpful", "is_error", "created_at")
    search_fields = ("question", "answer_excerpt", "client_ip", "feedback")
    ordering = ("-created_at", "-id")
    date_hierarchy = "created_at"
    list_per_page = 50
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
        color = {"rag": "#38bdf8", "gemini": "#a78bfa", "faq": "#10b981", "blocked": "#f87171"}.get(obj.mode, "#94a3b8")
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
# Feedback (base)
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


# ─────────────────────────────
# IngestHistory
# ─────────────────────────────
class IngestHistoryAdmin(admin.ModelAdmin):
    list_display = ("created_at", "keyword", "ingested_count", "total_candidates", "skipped_count", "failed_count")
    list_filter = ("keyword", "created_at")
    search_fields = ("keyword",)
    readonly_fields = ("created_at", "keyword", "total_candidates", "ingested_count", "skipped_count", "failed_count", "detail")


# ─────────────────────────────
# LegalConfig  (✅ 필드 변경에 안전하게 대응)
# ─────────────────────────────
_LC_FIELDS = _model_fieldnames(LegalConfig)


def _lc_pick(*names: str) -> tuple[str, ...]:
    return tuple(n for n in names if n in _LC_FIELDS)


class LegalConfigAdmin(admin.ModelAdmin):
    search_fields = _lc_pick(
        "policy_minimization",
        "policy_processing_location",
        "policy_change_note",
        "contact_link",
        "tos_version",
        "tester_version",
    )


# ─────────────────────────────
# RagChunk
# ─────────────────────────────
class RagChunkAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "doc_id", "dim", "created_at")
    list_filter = ("dim", "created_at")
    search_fields = ("title", "url", "doc_id", "text")
    readonly_fields = ("created_at",)
    ordering = ("-created_at",)


# ─────────────────────────────
# LiveChatSession
# ─────────────────────────────
_LCS_FIELDS = _model_fieldnames(LiveChatSession)


def _has(name: str) -> bool:
    return name in _LCS_FIELDS


def _pick(*names: str) -> list[str]:
    return [n for n in names if _has(n)]


class LiveChatSessionAdmin(admin.ModelAdmin):
    """
    LiveChatSession 목록 + 세션별 전체 대화 로그 보기 링크.
    """

    _ld: list[str] = []
    _ld += _pick("id", "code", "room")
    _ld += ["status_badge", "source", "user_name", "client_ip", "connected_at", "last_message_at"]
    _ld += _pick("created_at", "started_at", "ended_at", "processed_at")
    _ld += ["view_log_link"]
    list_display = tuple(_ld)

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
        "session_type",
        "session_note",
        "session_detail",
        "memo",
        "page_title",
        "page_path",
    )
    _ro += ["connected_at", "last_message_at", "view_log_link"]
    readonly_fields = tuple(_ro)

    list_filter = tuple(_pick("status", "created_at"))
    search_fields = tuple(_pick("room", "code", "session_note", "memo", "page_title", "page_path"))

    if _has("created_at"):
        ordering = ("-created_at", "-id")
        date_hierarchy = "created_at"
    else:
        ordering = ("-id",)

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

    @admin.display(description="대화 로그")
    def view_log_link(self, obj: LiveChatSession):
        try:
            url = reverse(f"{self.admin_site.name}:ragapp_livechatsession_log", args=[obj.pk])
        except Exception:
            url = f"/admin/ragapp/livechatsession/{obj.pk}/log/"
        return format_html('<a href="{}" target="_blank">대화 보기</a>', url)

    @admin.display(description="상태")
    def status_badge(self, obj: LiveChatSession):
        raw = getattr(obj, "status", "") or ""
        v = str(raw).lower()

        label = raw
        try:
            if hasattr(obj, "get_status_display"):
                label = obj.get_status_display() or raw
        except Exception:
            pass

        color = "#6b7280"
        if v in ("waiting", "대기"):
            color = "#38bdf8"
        elif v in ("active", "진행", "running"):
            color = "#22c55e"
        elif v in ("ended_need_save", "need_save", "ended", "종료", "done"):
            color = "#f97316"
        elif v in ("saved", "완료"):
            color = "#6366f1"
        elif v in ("deleted", "closed"):
            color = "#9ca3af"

        label = label or raw or "-"
        return format_html(
            '<span style="padding:2px 8px;border-radius:999px;background:{}20;color:{};font-size:11px;font-weight:600;">{}</span>',
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
        v = getattr(obj, "_last_message_at", None)
        if v:
            try:
                return timezone.localtime(v)
            except Exception:
                return v

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

        Msg = _get_livechat_message_model()
        if Msg is None:
            return qs

        mf = _model_fieldnames(Msg)

        try:
            if hasattr(LiveChatSession, "messages") and ("created_at" in mf):
                qs = qs.annotate(_last_message_at=Max("messages__created_at"))
                return qs
        except Exception:
            pass

        try:
            rel_name = None
            for rel in LiveChatSession._meta.related_objects:
                if getattr(rel, "related_model", None) == Msg:
                    rel_name = rel.get_accessor_name()
                    break
            if rel_name and ("created_at" in mf):
                qs = qs.annotate(_last_message_at=Max(f"{rel_name}__created_at"))
        except Exception:
            pass

        return qs

    def session_log_view(self, request: HttpRequest, session_id: int, *args, **kwargs):
        try:
            session = LiveChatSession.objects.get(pk=session_id)
        except LiveChatSession.DoesNotExist:
            return HttpResponse("세션을 찾을 수 없습니다.", status=404)

        Msg = _get_livechat_message_model()
        if Msg is None:
            return HttpResponse("메시지 모델을 찾을 수 없습니다.", status=500)

        mf = _model_fieldnames(Msg)

        qs = Msg.objects.all()
        if "session_id" in mf:
            qs = qs.filter(session_id=session.id)
        elif "session" in mf:
            qs = qs.filter(session=session)
        else:
            return HttpResponse("메시지 모델에 session FK가 없습니다.", status=500)

        if "created_at" in mf:
            msgs = qs.order_by("created_at")
        elif "ts" in mf:
            msgs = qs.order_by("ts")
        else:
            msgs = qs.order_by("id")

        from django.utils.html import escape

        rows: list[str] = []
        for m in msgs:
            ts = getattr(m, "created_at", None)
            if ts:
                try:
                    ts_str = timezone.localtime(ts).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    ts_str = str(ts)
            else:
                # ts(ms)만 있는 모델 대비
                ts_raw = getattr(m, "ts", None)
                ts_str = str(ts_raw) if ts_raw is not None else ""

            role = _msg_role(m)
            content = _msg_text(m)

            rows.append(
                "<tr>"
                f"<td class='ts'>{escape(ts_str)}</td>"
                f"<td class='role'>{escape(str(role))}</td>"
                f"<td class='msg'>{escape(str(content))}</td>"
                "</tr>"
            )

        tbody = "".join(rows) or "<tr><td colspan='3'>메시지가 없습니다.</td></tr>"
        title = f"Live chat session #{session.id} (room={session.room})"

        back_url = reverse(f"{self.admin_site.name}:ragapp_livechatsession_change", args=[session.id])

        html = f"""<!doctype html>
    <html lang="ko"><head>
    <meta charset="utf-8">
    <title>{escape(title)}</title>
    <style>
    body{{font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans KR",sans-serif;margin:0;padding:16px;background:#0f172a;color:#e5e7eb}}
    .wrap{{max-width:960px;margin:0 auto;background:#020617;border-radius:12px;padding:20px;border:1px solid #1f2937}}
    h1{{font-size:18px;margin:0 0 12px}}
    table{{width:100%;border-collapse:collapse;font-size:13px}}
    th,td{{border-bottom:1px solid #1f2937;padding:6px 8px;vertical-align:top}}
    th{{text-align:left;background:#020617;color:#9ca3af;font-weight:600}}
    .ts{{width:150px;white-space:nowrap;color:#9ca3af}}
    .role{{width:80px;font-weight:600}}
    .msg{{white-space:pre-wrap}}
    .meta{{font-size:12px;color:#9ca3af;margin:0 0 8px}}
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
    · <a href="{escape(back_url)}">세션으로 돌아가기</a>
    </div>
    <table>
    <thead><tr><th>시간</th><th>역할</th><th>내용</th></tr></thead>
    <tbody>{tbody}</tbody>
    </table>
    </div>
    </body></html>"""
        return HttpResponse(html)



# ─────────────────────────────
# TableSchema
# ─────────────────────────────
class TableSchemaAdmin(admin.ModelAdmin):
    list_display = ("table_name", "created_at", "updated_at")
    search_fields = ("table_name",)
    readonly_fields = ("columns", "column_types", "sample_rows", "created_at", "updated_at")
    ordering = ("table_name",)


# ─────────────────────────────
# AppLog
# ─────────────────────────────
class AppLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "level", "component", "trace_id", "op_name", "stage", "result_badge", "short_message")
    list_filter = ("level", "component", "created_at")
    search_fields = ("message", "component", "trace_id", "meta")
    date_hierarchy = "created_at"
    readonly_fields = ("created_at", "level", "component", "trace_id", "message", "meta")
    ordering = ("-created_at",)

    def _extra(self, obj):
        return getattr(obj, "meta", None) or {}

    def op_name(self, obj):
        extra = self._extra(obj)
        return extra.get("view") or extra.get("op") or obj.component or "-"

    op_name.short_description = "어디서"

    def stage(self, obj):
        extra = self._extra(obj)
        return extra.get("stage") or "-"

    stage.short_description = "단계"

    def result_badge(self, obj):
        extra = self._extra(obj)
        raw_result = extra.get("result")
        if isinstance(raw_result, dict):
            raw_result = (
                raw_result.get("result")
                or raw_result.get("status")
                or ("success" if raw_result.get("ok") is True else "")
            )
        elif isinstance(raw_result, bool):
            raw_result = "success" if raw_result else "fail"
        r = str(raw_result or "").lower()
        if not r:
            if obj.level in ("ERROR", "CRITICAL"):
                r = "fail"
            elif obj.level in ("WARN", "WARNING"):
                r = "warn"
            else:
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


# ─────────────────────────────
# QARAG Feedback / Proxy Feedbacks / Reviews
# ─────────────────────────────
class QaragFeedbackAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "is_helpful", "short_question", "session_id")
    list_filter = ("is_helpful", "legal_basis", "created_at")
    search_fields = ("question", "answer", "session_id", "client_ip")
    readonly_fields = ("created_at", "delete_at")

    def short_question(self, obj):
        txt = (obj.question or "").strip().replace("\n", " ")
        return (txt[:50] + "...") if len(txt) > 50 else txt

    short_question.short_description = "질문 미리보기"


class RagSearchFeedbackAdmin(admin.ModelAdmin):
    list_display = ("id", "short_q", "short_a", "is_helpful", "answer_type", "created_at")
    list_filter = ("is_helpful", "answer_type", "created_at")
    search_fields = ("question", "answer")
    readonly_fields = ("created_at",)

    def short_q(self, obj):
        q = (obj.question or "").strip()
        return q if len(q) <= 40 else q[:37] + "…"

    def short_a(self, obj):
        a = (obj.answer or "").strip()
        return a if len(a) <= 40 else a[:37] + "…"


class WebFeedbackAdmin(admin.ModelAdmin):
    list_display = ("id", "short_q", "short_a", "is_helpful", "created_at")
    list_filter = ("is_helpful", "created_at")
    search_fields = ("question", "answer")
    readonly_fields = ("created_at",)

    def short_q(self, obj):
        q = (obj.question or "").strip()
        return q if len(q) <= 40 else q[:37] + "…"

    def short_a(self, obj):
        a = (obj.answer or "").strip()
        return a if len(a) <= 40 else a[:37] + "…"


class FeedbackLogAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "answer_type", "channel_label", "helpful_icon", "short_question")
    list_filter = ("answer_type", "helpful", "from_ui", "stage")
    search_fields = ("question", "answer", "comment")

    def helpful_icon(self, obj):
        if obj.helpful is True:
            return "👍"
        if obj.helpful is False:
            return "👎"
        return "-"

    helpful_icon.short_description = "평가"


class FeedbackReviewAdmin(admin.ModelAdmin):
    list_display = ("id", "status", "owner", "feedback_channel", "feedback_short_q", "created_at", "updated_at")
    list_filter = ("status", "owner")
    search_fields = ("plan", "resolution", "feedback__question", "feedback__answer")

    def feedback_channel(self, obj):
        return obj.feedback.get_answer_type_display()

    def feedback_short_q(self, obj):
        return obj.feedback.short_question()


# ─────────────────────────────
# TableSearchRule
# ─────────────────────────────
class TableSearchRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "table_name", "is_active", "min_sim", "hard_filter_enabled", "updated_at")
    list_filter = ("is_active", "hard_filter_enabled")
    search_fields = ("name", "table_name")
    ordering = ("-updated_at", "-id")
    readonly_fields = ("created_at", "updated_at")


# ─────────────────────────────
# (선택) MediaAsset / TableDataset
# ─────────────────────────────
if _HAS_MEDIA_MODELS:

    class MediaAssetAdmin(admin.ModelAdmin):
        change_list_template = "admin/ragapp/mediaasset/change_list.html"
        list_display = ("id", "file", "caption", "mime", "size_display", "index_status", "created_at")
        search_fields = ("caption", "file")
        list_filter = ("mime", "created_at", "indexed_at")
        readonly_fields = ("mime", "size", "sha256", "chroma_id", "indexed_at", "created_at", "updated_at")
        date_hierarchy = "created_at"
        ordering = ("-created_at",)

        @admin.display(description="크기")
        def size_display(self, obj):
            size = int(obj.size or 0)
            if size >= 1024 * 1024:
                return f"{size / (1024 * 1024):.1f} MB"
            if size >= 1024:
                return f"{size / 1024:.1f} KB"
            return f"{size} B"

        @admin.display(description="AI 검색", boolean=True)
        def index_status(self, obj):
            return bool(obj.chroma_id and obj.indexed_at)

        def get_urls(self):
            urls = super().get_urls()
            custom = [
                path("search/", self.admin_site.admin_view(self.search_view), name="mediaasset_search"),
            ]
            return custom + urls

        def search_view(self, request: HttpRequest):
            ns = self.admin_site.name
            back = reverse(f"{ns}:ragapp_mediaasset_changelist")
            q = (request.POST.get("q") or "").strip()
            try:
                k = max(1, min(int(request.POST.get("k") or 8), 50))
            except Exception:
                k = 8
            results = []
            error = ""
            if request.method == "POST" and q:
                try:
                    from ragapp.services.vertex_embed import embed_text_mm
                    from ragapp.services.chroma_media import search_images_by_text_embedding

                    response = search_images_by_text_embedding(text_embedding=embed_text_mm(q), k=k) or {}
                    ids = (response.get("ids") or [[]])[0]
                    metas = (response.get("metadatas") or [[]])[0]
                    docs = (response.get("documents") or [[]])[0]
                    distances = (response.get("distances") or [[]])[0]
                    for index, pid in enumerate(ids):
                        meta = metas[index] if index < len(metas) else {}
                        results.append({
                            "rank": index + 1,
                            "pid": pid,
                            "caption": docs[index] if index < len(docs) else "",
                            "path": (meta or {}).get("path", ""),
                            "distance": distances[index] if index < len(distances) else None,
                        })
                except Exception as exc:
                    log.exception("MediaAsset semantic search failed")
                    error = f"AI 유사 검색을 완료하지 못했습니다: {exc}"

            context = {
                **self.admin_site.each_context(request),
                "title": "미디어 자산 AI 유사 검색",
                "opts": self.model._meta,
                "back_url": back,
                "q": q,
                "k": k,
                "results": results,
                "error": error,
            }
            return render(request, "admin/ragapp/mediaasset/search.html", context)

        def save_model(self, request, obj, form, change):
            upload = getattr(obj, "file", None)
            if upload:
                obj.mime = mimetypes.guess_type(upload.name)[0] or "application/octet-stream"
                try:
                    obj.size = int(upload.size or 0)
                except Exception:
                    pass
                try:
                    upload.open("rb")
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: upload.read(1024 * 1024), b""):
                        digest.update(chunk)
                    obj.sha256 = digest.hexdigest()
                    upload.seek(0)
                except Exception:
                    log.exception("MediaAsset metadata calculation failed")
            super().save_model(request, obj, form, change)

    class TableDatasetAdmin(admin.ModelAdmin):
        list_display = ("id", "table_name", "csv", "row_count", "indexed_at")
        search_fields = ("table_name", "csv")


# ─────────────────────────────
# ✅ 등록: admin.site + rag_admin_site
# ─────────────────────────────
# 기본 admin.site
_safe_register(admin.site, MyLog, MyLogAdmin, force=True)
_safe_register(admin.site, RagSetting, RagSettingAdmin, force=True)
_safe_register(admin.site, ChatQueryLog, ChatQueryLogAdmin, force=True)
_safe_register(admin.site, FaqEntry, FaqEntryAdmin, force=True)
_safe_register(admin.site, Feedback, FeedbackAdmin, force=True)
_safe_register(admin.site, IngestHistory, IngestHistoryAdmin, force=True)
_safe_register(admin.site, TableSchema, TableSchemaAdmin, force=True)
_safe_register(admin.site, RagChunk, RagChunkAdmin, force=True)
_safe_register(admin.site, AppLog, AppLogAdmin, force=True)
_safe_register(admin.site, LegalConfig, LegalConfigAdmin, force=True)
_safe_register(admin.site, LiveChatSession, LiveChatSessionAdmin, force=True)

_safe_register(admin.site, TableSearchRule, TableSearchRuleAdmin, force=True)
_safe_register(admin.site, QaragFeedback, QaragFeedbackAdmin, force=True)
_safe_register(admin.site, RagSearchFeedback, RagSearchFeedbackAdmin, force=True)
_safe_register(admin.site, WebFeedback, WebFeedbackAdmin, force=True)
_safe_register(admin.site, FeedbackLog, FeedbackLogAdmin, force=True)
_safe_register(admin.site, FeedbackReview, FeedbackReviewAdmin, force=True)

if _HAS_MEDIA_MODELS:
    _safe_register(admin.site, MediaAsset, MediaAssetAdmin, force=True)   # type: ignore[name-defined]
    _safe_register(admin.site, TableDataset, TableDatasetAdmin, force=True)  # type: ignore[name-defined]

# rag_admin_site (있을 때만)
if rag_admin_site is not None:
    _safe_register(rag_admin_site, MyLog, MyLogAdmin, force=True)
    _safe_register(rag_admin_site, RagSetting, RagSettingAdmin, force=True)
    _safe_register(rag_admin_site, ChatQueryLog, ChatQueryLogAdmin, force=True)
    _safe_register(rag_admin_site, FaqEntry, FaqEntryAdmin, force=True)
    _safe_register(rag_admin_site, Feedback, FeedbackAdmin, force=True)
    _safe_register(rag_admin_site, IngestHistory, IngestHistoryAdmin, force=True)
    _safe_register(rag_admin_site, TableSchema, TableSchemaAdmin, force=True)
    _safe_register(rag_admin_site, RagChunk, RagChunkAdmin, force=True)
    _safe_register(rag_admin_site, AppLog, AppLogAdmin, force=True)
    _safe_register(rag_admin_site, LegalConfig, LegalConfigAdmin, force=True)
    _safe_register(rag_admin_site, LiveChatSession, LiveChatSessionAdmin, force=True)

    _safe_register(rag_admin_site, TableSearchRule, TableSearchRuleAdmin, force=True)
    _safe_register(rag_admin_site, QaragFeedback, QaragFeedbackAdmin, force=True)
    _safe_register(rag_admin_site, RagSearchFeedback, RagSearchFeedbackAdmin, force=True)
    _safe_register(rag_admin_site, WebFeedback, WebFeedbackAdmin, force=True)
    _safe_register(rag_admin_site, FeedbackLog, FeedbackLogAdmin, force=True)
    _safe_register(rag_admin_site, FeedbackReview, FeedbackReviewAdmin, force=True)

    if _HAS_MEDIA_MODELS:
        _safe_register(rag_admin_site, MediaAsset, MediaAssetAdmin, force=True)   # type: ignore[name-defined]
        _safe_register(rag_admin_site, TableDataset, TableDatasetAdmin, force=True)  # type: ignore[name-defined]
