# ragapp/models.py
from __future__ import annotations

from datetime import date, timedelta

from django.conf import settings
import secrets
from django.db import models
from django.utils import timezone
from django.utils.crypto import get_random_string

# ✅ LiveChatMessage/ChatEvidence 등은 "한 곳(models_chat_retention.py)"에서만 정의하고
#   여기서는 import 해서 ragapp.models 네임스페이스로 재-export 한다.
from .models_chat_retention import (  # noqa: F401
    LiveChatMessage,
    ChatEvidence,
    PurgeRun,
    RetentionClass,
    compute_purge_at,
)


# ============================================================================
# 공통: 보존 기간 계산 유틸
# ============================================================================
def _retention_days(name: str, fallback: int = 0) -> int:
    """
    settings 에서 일(day) 단위 보존 기간 값을 가져옵니다.

    - name 에 해당하는 값이 있으면 우선 사용합니다.
    - 없으면 RETENTION_DAYS 를 찾고, 그것도 없으면 fallback 값을 씁니다.
    """
    return int(getattr(settings, name, None) or getattr(settings, "RETENTION_DAYS", fallback) or 0)


def _compute_delete_at(created_at, days: int):
    """
    생성 시각 + 보존 일수를 더해 자동 삭제 예정 시각을 계산합니다.
    days 가 0 이하이면 None 을 돌려줍니다.
    """
    if not created_at or not days or days <= 0:
        return None
    return created_at + timedelta(days=days)


# ============================================================================
# RAG 설정 / 관리 로그 / FAQ
# ============================================================================
class RagSetting(models.Model):
    """
    RAG 검색과 인덱싱(자료 쌓기)에 필요한 기본 설정 모음입니다.

    /ragadmin/ 화면에서 값들을 보고 수정할 수 있고,
    보통은 1개만 쓰되 필요하면 여러 버전을 쌓을 수도 있습니다.
    """

    # Chroma 저장 위치
    chroma_db_dir = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text="로컬 Chroma DB 폴더 경로",
    )
    chroma_collection = models.CharField(
        max_length=128,
        blank=True,
        default="web_rag",
        help_text="Chroma 컬렉션 이름 (기존 컬렉션에 계속 append)",
    )

    # 동작 옵션
    auto_ingest_after_gemini = models.BooleanField(
        default=True,
        help_text="웹 검색(Gemini) 직후 자동으로 인덱싱까지 수행할지 여부",
    )
    web_ingest_to_chroma = models.BooleanField(
        default=True,
        help_text="검색 결과를 Chroma에 저장(인덱싱)할지 여부",
    )
    crawl_answer_links = models.BooleanField(
        default=True,
        help_text="답변 본문 속 URL까지 따라가서 본문 크롤링/인덱싱할지",
    )

    # RAG / 뉴스 관련 파라미터
    rag_query_topk = models.IntegerField(default=5, help_text="RAG 1차 retrieval top-k")
    rag_fallback_topk = models.IntegerField(default=12, help_text="RAG 2차(확장) retrieval top-k")
    rag_max_sources = models.IntegerField(default=8, help_text="최종 답변에 노출할 근거 source 개수 상한")
    news_topk = models.IntegerField(default=5, help_text="구글 뉴스 RSS에서 긁어올 기사 수")

    # 레코드 갱신 시각
    updated_at = models.DateTimeField(auto_now=True, help_text="이 레코드를 마지막으로 저장한 시각")

    def __str__(self):
        return f"RagSetting#{self.pk} ({self.chroma_collection})"


class MyLog(models.Model):
    """
    관리자 작업 내역을 간단히 남겨 두는 로그입니다.

    예) /ragadmin/crawl-news/ 에서 크롤링 실행, 수동 인덱싱, 테스트 호출 등.
    """

    created_at = models.DateTimeField(auto_now_add=True, help_text="로그 생성 시각")

    mode_text = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="무슨 작업인지. 예: 'admin_crawl', 'api_ingest' 등",
    )
    query = models.TextField(blank=True, default="", help_text="사용된 키워드/질문 등")
    ok_flag = models.BooleanField(default=True, help_text="성공 여부")

    remote_addr_text = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="요청자 IP 등",
    )
    extra_json = models.JSONField(blank=True, default=dict, help_text="결과 요약, 에러 메시지, 저장 chunk 수 등 자유롭게")

    def __str__(self):
        ts = timezone.localtime(self.created_at).strftime("%Y-%m-%d %H:%M")
        status = "OK" if self.ok_flag else "FAIL"
        return f"[{status}] {self.mode_text} '{self.query[:30]}' @ {ts}"


# ============================================================================
# 동의(Consent) 및 법적 문서 버전 관리
# ============================================================================
class LegalDocumentVersion(models.Model):
    """
    개인정보처리방침, 이용약관 등 문서 버전 이력 테이블.
    """

    slug = models.SlugField(max_length=64, db_index=True, help_text="예: privacy, terms")
    version = models.CharField(max_length=32, db_index=True, help_text="예: v1, 2025-11-02")
    title = models.CharField(max_length=200)
    content_md = models.TextField(blank=True, default="", help_text="문서 원문(Markdown 등)")
    published_at = models.DateTimeField(default=timezone.now, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        unique_together = [("slug", "version")]
        ordering = ["-published_at", "-id"]

    def __str__(self):
        return f"{self.slug}@{self.version}"


class ConsentLog(models.Model):
    """
    사용자 동의(필수/선택)에 대한 세션 단위 기록.
    """

    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    session_key = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="Django 세션 키(증빙용)",
    )
    ip_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="요청자 IP의 해시/익명화 표현(원시 IP 미저장)",
    )
    user_agent = models.CharField(max_length=400, blank=True, default="", help_text="User-Agent (최대 400자)")

    consent_type = models.CharField(
        max_length=32,
        default="required",
        help_text="required / optional / marketing 등 구분",
        db_index=True,
    )

    document = models.ForeignKey(
        LegalDocumentVersion,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="consent_logs",
        help_text="동의 당시 사용된 정책/약관 버전(선택)",
    )
    version = models.CharField(
        max_length=32,
        default="v1",
        help_text="프런트에서 전달한 버전 문자열(문서 연결이 안 될 때 사용)",
        db_index=True,
    )
    scope = models.CharField(
        max_length=32,
        default="session",
        help_text="세션/계정/기간 등 범위 표기용",
        db_index=True,
    )

    artifact_hash = models.CharField(max_length=128, blank=True, default="", help_text="동의 스냅샷/아티팩트 해시 (선택)")
    extra = models.JSONField(blank=True, null=True, help_text="프론트에서 전달한 부가 데이터(FormData/JSON)")

    legal_hold = models.BooleanField(default=False, db_index=True, help_text="법적 보존 필요 시 True (자동 파기 제외)")
    delete_at = models.DateTimeField(null=True, blank=True, db_index=True, help_text="정기 파기 대상 시각")

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        ts = timezone.localtime(self.created_at).strftime("%Y-%m-%d %H:%M")
        return f"[self.consent_type/{self.version}] {self.session_key[:8]}… @ {ts}"

    def save(self, *args, **kwargs):
        if not self.delete_at and not self.legal_hold:
            days = _retention_days("RETENTION_DAYS_CONSENT", fallback=0)
            self.delete_at = _compute_delete_at(self.created_at, days)
        super().save(*args, **kwargs)


# ============================================================================
# 챗봇/검색 질의·응답 로그 + 별도 피드백 테이블
# ============================================================================
class ChatQueryLog(models.Model):
    MODE_CHOICES = [
        ("faq", "FAQ (qa_data.py)"),
        ("rag", "RAG 검색"),
        ("gemini", "Gemini / 웹 검색"),
        ("blocked", "차단/정책 위반"),
    ]

    LEGAL_BASIS_CHOICES = [
        ("consent", "동의(Consent)"),
        ("contract", "계약 이행(Contract)"),
        ("legitimate_interest", "정당한 이익(Legitimate Interest)"),
        ("legal_obligation", "법적 의무(Legal Obligation)"),
        ("other", "기타"),
    ]

    CHANNEL_CHOICES = [
        ("qarag", "QARAG 위젯"),
        ("live_console", "실시간 상담 콘솔"),
        ("api", "외부 API/연동"),
        ("system", "시스템/배치"),
    ]

    ROLE_CHOICES = [("user", "사용자"), ("assistant", "봇/상담원"), ("system", "시스템")]

    MESSAGE_TYPE_CHOICES = [("query", "질문"), ("answer", "답변"), ("note", "노트/코멘트"), ("error", "에러")]

    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    session_id = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="동일 브라우저/상담 세션을 구분하는 ID (QARAG/실시간 콘솔 공용)",
    )

    channel = models.CharField(
        max_length=32,
        choices=CHANNEL_CHOICES,
        blank=True,
        default="qarag",
        db_index=True,
        help_text="메시지가 생성된 채널(위젯/실시간콘솔/API 등)",
    )

    mode = models.CharField(max_length=20, choices=MODE_CHOICES, help_text="어떤 엔진 또는 단계가 최종 응답했는지", db_index=True)

    role = models.CharField(max_length=16, choices=ROLE_CHOICES, blank=True, default="user", db_index=True)

    message_type = models.CharField(max_length=16, choices=MESSAGE_TYPE_CHOICES, blank=True, default="query", db_index=True)

    question = models.TextField(help_text="사용자가 실제로 입력한 질문 원문")

    content = models.TextField(
        blank=True,
        default="",
        help_text=(
            "단일 메시지의 원문(질문/답변/노트 등). "
            "QARAG/실시간 상담 콘솔의 공용 대화 로그용 필드로 사용. "
            "기존 question/answer_excerpt와 병행 가능."
        ),
    )

    answer_excerpt = models.TextField(blank=True, help_text="사용자에게 돌려준 답변의 앞부분 (요약/미리보기)")

    client_ip = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="요청자 IP의 해시/익명화 표현(원시 IP 미저장)",
    )

    is_error = models.BooleanField(default=False, help_text="이 호출이 실패했는지 여부 (예외 등)")
    error_msg = models.TextField(blank=True, help_text="에러가 났다면 스택 대신 요약 메시지")

    was_helpful = models.BooleanField(default=None, null=True, blank=True, help_text="True/False/아직없음")
    feedback = models.TextField(blank=True, default="", help_text="유저가 남긴 자유 코멘트 (선택)")

    sources = models.JSONField(blank=True, default=list, help_text="참고 소스 목록 [{title,url},...]")
    meta = models.JSONField(blank=True, default=dict, help_text="요청/클라이언트 메타 (UA, path, 세션정보 등)")

    legal_basis = models.CharField(
        max_length=32,
        choices=LEGAL_BASIS_CHOICES,
        default="consent",
        db_index=True,
        help_text="데이터 처리의 법적 근거",
    )
    consent_version = models.CharField(max_length=32, blank=True, default="", help_text="동의 문구/문서 버전 문자열(선택)")
    consent_log = models.ForeignKey(
        ConsentLog,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="chat_logs",
        help_text="실제 동의 증빙 레코드(선택)",
    )

    legal_hold = models.BooleanField(default=False, db_index=True, help_text="법적 보존 필요 시 True (자동 파기 제외)")
    delete_at = models.DateTimeField(null=True, blank=True, db_index=True, help_text="보존기간 경과 시점(RETENTION_DAYS_CHATLOG)")

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["session_id", "created_at"]),
            models.Index(fields=["channel", "created_at"]),
            models.Index(fields=["mode", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"[{self.created_at:%Y-%m-%d %H:%M:%S}] {self.channel}/{self.role} - {self.question[:40]}"

    def short_q(self):
        txt = self.question or ""
        return txt[:50] + ("..." if len(txt) > 50 else "")

    short_q.short_description = "질문 미리보기"

    def short_a(self):
        txt = self.answer_excerpt or ""
        return txt[:50] + ("..." if len(txt) > 50 else "")

    short_a.short_description = "답변 미리보기"

    def save(self, *args, **kwargs):
        if not self.delete_at and not self.legal_hold:
            days = _retention_days("RETENTION_DAYS_CHATLOG", fallback=0)
            self.delete_at = _compute_delete_at(self.created_at, days)
        super().save(*args, **kwargs)


class QaragFeedback(models.Model):
    LEGAL_BASIS_CHOICES = ChatQueryLog.LEGAL_BASIS_CHOICES

    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    chat_log = models.ForeignKey(
        "ragapp.ChatQueryLog",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="qarag_feedbacks",
        help_text="피드백 대상이 된 질문/답변 로그 (대개 assistant 메시지)",
    )

    session_id = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="QARAG 위젯 session_id 또는 프론트에서 넘긴 세션 식별자",
    )

    question = models.TextField(blank=True, default="", help_text="피드백 대상이 된 질문(요약 또는 원문)")
    answer = models.TextField(blank=True, default="", help_text="피드백 대상이 된 답변(요약 또는 전체)")

    is_helpful = models.BooleanField(null=True, blank=True, db_index=True, help_text="👍 True / 👎 False / 미선택 null")
    comment = models.TextField(blank=True, default="", help_text="사용자가 적어 준 상세 코멘트 (선택)")

    client_ip = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="요청자 IP의 해시/익명화 표현(원시 IP 미저장)",
    )

    legal_basis = models.CharField(
        max_length=32,
        choices=LEGAL_BASIS_CHOICES,
        default="consent",
        db_index=True,
        help_text="데이터 처리의 법적 근거",
    )
    consent_version = models.CharField(max_length=32, blank=True, default="", help_text="동의 문구/문서 버전 문자열(선택)")
    consent_log = models.ForeignKey(
        ConsentLog,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="qarag_feedbacks",
        help_text="실제 동의 증빙 레코드(선택)",
    )

    legal_hold = models.BooleanField(default=False, db_index=True, help_text="법적 보존 필요 시 True (자동 파기 제외)")
    delete_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="RETENTION_DAYS_QARAG_FEEDBACK 또는 RETENTION_DAYS_FEEDBACK",
    )

    extra = models.JSONField(blank=True, null=True, help_text="프런트에서 넘기는 부가 데이터(선택)")

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "질문 챗봇 피드백"
        verbose_name_plural = "질문 챗봇 피드백"
        indexes = [
            models.Index(fields=["session_id", "created_at"]),
            models.Index(fields=["is_helpful", "created_at"]),
        ]

    def __str__(self) -> str:
        base_q = (self.question or "").strip().replace("\n", " ")
        if len(base_q) > 50:
            base_q = base_q[:50] + "..."
        thumb = "👍" if self.is_helpful else "👎"
        return f"[QARAG/{thumb}] {base_q}"

    def save(self, *args, **kwargs):
        if not self.delete_at and not self.legal_hold:
            days = _retention_days("RETENTION_DAYS_QARAG_FEEDBACK", fallback=0)
            if not days:
                days = _retention_days("RETENTION_DAYS_FEEDBACK", fallback=0)
            self.delete_at = _compute_delete_at(self.created_at, days)
        super().save(*args, **kwargs)


class FaqEntry(models.Model):
    question = models.TextField("질문", help_text="사용자가 물어볼 수 있는 형태 그대로 적어주세요.")
    answer = models.TextField("답변", help_text="우리가 공식적으로 안내할 답변")
    is_active = models.BooleanField("활성화 여부", default=True)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "FAQ 항목"
        verbose_name_plural = "FAQ 항목들"
        ordering = ["-updated_at", "-created_at"]

    def __str__(self):
        short_q = (self.question or "").strip().replace("\n", " ")
        if len(short_q) > 50:
            short_q = short_q[:50] + "…"
        return short_q


class Feedback(models.Model):
    ANSWER_TYPE_CHOICES = [("gemini", "Gemini / Web 요약"), ("rag", "RAG 답변"), ("other", "기타 / 기타 응답")]
    LEGAL_BASIS_CHOICES = ChatQueryLog.LEGAL_BASIS_CHOICES

    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    question = models.TextField(blank=True, default="")
    answer = models.TextField(blank=True, default="")

    answer_type = models.CharField(max_length=20, choices=ANSWER_TYPE_CHOICES, default="other", db_index=True)
    is_helpful = models.BooleanField(default=True, db_index=True)
    sources_json = models.JSONField(blank=True, null=True)

    client_ip = models.CharField(max_length=64, blank=True, default="", db_index=True, help_text="요청자 IP 해시(원시 IP 미저장)")

    legal_basis = models.CharField(max_length=32, choices=LEGAL_BASIS_CHOICES, default="consent", db_index=True)
    consent_version = models.CharField(max_length=32, blank=True, default="")
    consent_log = models.ForeignKey(ConsentLog, null=True, blank=True, on_delete=models.SET_NULL, related_name="feedbacks")

    legal_hold = models.BooleanField(default=False, db_index=True)
    delete_at = models.DateTimeField(null=True, blank=True, db_index=True, help_text="RETENTION_DAYS_FEEDBACK")

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        base_q = (self.question or "").strip().replace("\n", " ")
        if len(base_q) > 50:
            base_q = base_q[:50] + "..."
        thumb = "👍" if self.is_helpful else "👎"
        return f"[{self.answer_type}/{thumb}] {base_q}"

    def save(self, *args, **kwargs):
        if not self.delete_at and not self.legal_hold:
            days = _retention_days("RETENTION_DAYS_FEEDBACK", fallback=0)
            self.delete_at = _compute_delete_at(self.created_at, days)
        super().save(*args, **kwargs)


class IngestHistory(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    keyword = models.CharField(max_length=200)

    total_candidates = models.IntegerField(default=0)
    ingested_count = models.IntegerField(default=0)
    skipped_count = models.IntegerField(default=0)
    failed_count = models.IntegerField(default=0)

    detail = models.JSONField(blank=True, null=True)

    def __str__(self):
        return f"[{self.created_at:%Y-%m-%d %H:%M}] {self.keyword} {self.ingested_count}/{self.total_candidates} ingested"


class FaqSuggestProxy(FaqEntry):
    class Meta:
        proxy = True
        verbose_name = "FAQ 후보 추천"
        verbose_name_plural = "FAQ 후보 추천"
        app_label = "ragapp"


class CrawlNewsProxy(RagSetting):
    class Meta:
        proxy = True
        verbose_name = "뉴스 크롤 & 인덱싱"
        verbose_name_plural = "뉴스 크롤 & 인덱싱"
        app_label = "ragapp"


# ============================================================================
# 데이터 권리행사 요청(삭제/열람 등) + 감사 로그
# ============================================================================
class DataErasureTicket(models.Model):
    STATUS_CHOICES = [("open", "접수"), ("processing", "처리중"), ("done", "완료"), ("rejected", "거절")]

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    channel = models.CharField(max_length=32, default="web", db_index=True)
    requester_token = models.CharField(max_length=128, blank=True, default="")
    target_ip_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    scope = models.CharField(max_length=64, default="chatlog,feedback,consent")
    reason = models.TextField(blank=True, default="")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="open", db_index=True)
    processed_by = models.CharField(max_length=64, blank=True, default="system")
    result_json = models.JSONField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"[{self.status}] DSR for {self.target_ip_hash[:8]}…"


class AuditEvent(models.Model):
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    actor = models.CharField(max_length=64, default="system", db_index=True)
    action = models.CharField(max_length=64, db_index=True)
    target_model = models.CharField(max_length=64, blank=True, default="", db_index=True)
    target_pk = models.CharField(max_length=64, blank=True, default="", db_index=True)
    notes = models.TextField(blank=True, default="")
    extra = models.JSONField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.action} by {self.actor} @ {self.created_at:%Y-%m-%d %H:%M}"


# ============================================================================
# 법 관련 설정 + HTML sanitize 유틸
# ============================================================================
def sanitize_legal_html(value: str) -> str:
    if not value:
        return ""
    try:
        import bleach  # type: ignore

        allowed_tags = [
            "a",
            "b",
            "strong",
            "i",
            "em",
            "u",
            "br",
            "p",
            "ul",
            "ol",
            "li",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "code",
            "pre",
            "blockquote",
            "span",
            "div",
        ]
        allowed_attrs = {"a": ["href", "title", "target", "rel"], "span": ["data-bind"], "div": ["data-bind"]}
        return bleach.clean(value, tags=allowed_tags, attributes=allowed_attrs, strip=True)
    except Exception:
        return value


class LegalConfig(models.Model):
    service_name = models.CharField(max_length=120, blank=True, default="")
    effective_date = models.DateField(blank=True, null=True)

    operator_name = models.CharField(max_length=120, blank=True, default="")
    contact_email = models.EmailField(blank=True, default="")
    contact_phone = models.CharField(max_length=50, blank=True, default="")

    consent_gate_enabled = models.BooleanField(default=True)

    guide_html = models.TextField(blank=True, default="")
    privacy_html = models.TextField(blank=True, default="")
    cross_border_html = models.TextField(blank=True, default="")
    tester_html = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "법적 설정"
        verbose_name_plural = "법적 설정"

    def __str__(self):
        return f"[법적설정] {self.service_name} ({self.effective_date})"

    @property
    def sanitized_privacy_html(self) -> str:
        return sanitize_legal_html(self.privacy_html)

    @property
    def sanitized_cross_border_html(self) -> str:
        return sanitize_legal_html(self.cross_border_html)

    @property
    def sanitized_tester_html(self) -> str:
        return sanitize_legal_html(self.tester_html)

    @classmethod
    def get_solo(cls) -> "LegalConfig":
        obj = cls.objects.first()
        if obj:
            return obj
        return cls.objects.create()


class RagChunk(models.Model):
    id = models.BigAutoField(primary_key=True)
    unique_hash = models.CharField(max_length=64, unique=True, db_index=True)
    doc_id = models.CharField(max_length=191, blank=True, db_index=True)
    url = models.URLField(blank=True)
    title = models.CharField(max_length=500, blank=True)
    text = models.TextField()
    meta = models.JSONField(default=dict, blank=True)

    embedding = models.BinaryField()
    dim = models.PositiveSmallIntegerField()

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["doc_id"]), models.Index(fields=["created_at"])]

    def __str__(self):
        return f"{self.title or '(no title)'} - {self.url or ''}"


class MediaAsset(models.Model):
    file = models.FileField(upload_to="media_assets/%Y/%m/")
    caption = models.CharField(max_length=255, blank=True)
    mime = models.CharField(max_length=100, blank=True)
    size = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True)
    chroma_id = models.CharField(max_length=200, blank=True)
    indexed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"[{self.id}] {self.file.name}"


class TableDataset(models.Model):
    table_name = models.CharField(max_length=128)
    csv = models.FileField(upload_to="table_datasets/%Y/%m/")
    row_count = models.IntegerField(default=0)
    indexed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.table_name} ({self.csv.name})"


class LiveChatRoom(models.Model):
    STATUS_CHOICES = [("waiting", "대기"), ("active", "진행 중"), ("closed", "종료")]

    room_id = models.CharField(max_length=64, unique=True)
    client_label = models.CharField(max_length=100, blank=True)
    last_question = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="waiting")
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="livechat_rooms",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now_add=True)  # (원본 유지)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.room_id} ({self.get_status_display()})"


class LiveChatSession(models.Model):
    """
    실시간 상담 세션(1명의 사용자와의 상담 1건)
    - QARAG → /api/livechat/request/ 로 생성
    - 상담사 콘솔에서 /ragadmin/live-chat/ 으로 조회/처리
    """

    class Status(models.TextChoices):
        WAITING = "waiting", "대기"
        ACTIVE = "active", "진행"
        ENDED = "ended", "종료"
        DONE = "done", "기록 완료"
        DELETED = "deleted", "삭제됨"

    # 선택적으로 쓸 수 있는 세션 유형 라벨 맵(없으면 원문 그대로)
    SESSION_TYPE_LABELS = {
        "general": "일반 문의",
        "tech": "기술 문의",
        "billing": "결제/요금 문의",
    }

    # 식별
    room = models.CharField(max_length=128, db_index=True)
    code = models.CharField(max_length=32, blank=True, null=True, db_index=True)

    # 상태
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.WAITING,
        db_index=True,
    )

    # 상담 메타(후처리용)
    session_type = models.CharField(max_length=64, blank=True, null=True)
    session_note = models.TextField(blank=True, null=True)
    session_detail = models.TextField(blank=True, null=True)

    # 레거시/표시용(템플릿에서 memo를 직접 쓰는 경우 대비)
    memo = models.TextField(blank=True, null=True)

    # 유입 페이지(있으면 편함)
    page_title = models.CharField(max_length=200, blank=True, null=True)
    page_path = models.CharField(max_length=300, blank=True, null=True)

    # 상담사 배정(있으면 _has_unlogged_session 에서 상담사별 필터용)
    assigned_staff = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="livechat_sessions",
    )

    # 타임스탬프
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    requested_at = models.DateTimeField(blank=True, null=True, db_index=True)
    started_at = models.DateTimeField(blank=True, null=True, db_index=True)
    ended_at = models.DateTimeField(blank=True, null=True, db_index=True)
    processed_at = models.DateTimeField(blank=True, null=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["room", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"LiveChatSession(id={self.id}, room={self.room}, status={self.status})"

    # ─────────────────────────────────────────
    # 상태 전이 헬퍼
    # ─────────────────────────────────────────
    def mark_waiting(self):
        self.status = self.Status.WAITING
        if not self.requested_at:
            self.requested_at = timezone.now()

    def mark_active(self):
        self.status = self.Status.ACTIVE
        if not self.started_at:
            self.started_at = timezone.now()

    def mark_ended(self):
        self.status = self.Status.ENDED
        if not self.ended_at:
            self.ended_at = timezone.now()

    def mark_done(self):
        self.status = self.Status.DONE
        now = timezone.now()
        if not self.ended_at:
            self.ended_at = now
        self.processed_at = now

    @property
    def status_priority(self) -> int:
        # 정렬용 우선순위 (기록 필요/진행중/대기/기록완료 순)
        return {
            self.Status.ENDED: 0,   # 종료(기록 필요)
            self.Status.ACTIVE: 1,
            self.Status.WAITING: 2,
            self.Status.DONE: 3,
            self.Status.DELETED: 9,
        }.get(self.status, 9)

    @property
    def note(self) -> str:
        # 예전 템플릿/코드에서 note를 쓰던 경우 대응
        return (self.session_note or "").strip() or (self.memo or "").strip()

    def get_session_type_display(self) -> str:
        # choices 없어도 템플릿에서 안전하게 라벨 표시
        v = (self.session_type or "").strip()
        return self.SESSION_TYPE_LABELS.get(v, v)

    # ─────────────────────────────────────────
    # code 자동 발급 (충돌 방지 + update_fields 대응)
    # ─────────────────────────────────────────
    def _gen_code(self) -> str:
        return "lc-" + secrets.token_hex(4)

    def save(self, *args, **kwargs):
        if not self.code:
            Model = type(self)

            # 충돌 가능성 거의 없지만 “진짜로” 안전하게
            for _ in range(8):
                cand = self._gen_code()
                if not Model.objects.filter(code=cand).exists():
                    self.code = cand
                    break
            else:
                self.code = "lc-" + secrets.token_hex(8)

            # update_fields로 저장하는 경우 code도 같이 포함시켜 유실 방지
            uf = kwargs.get("update_fields")
            if uf is not None:
                kwargs["update_fields"] = list(set(uf) | {"code"})

        super().save(*args, **kwargs)


# ✅✅✅ LiveChatMessage 모델은 여기서 정의하지 않는다.
# (models_chat_retention.py 의 LiveChatMessage 하나만 사용)


class TableSchema(models.Model):
    table_name = models.CharField(max_length=128, unique=True)
    columns = models.JSONField(default=list)
    column_types = models.JSONField(default=dict, blank=True)
    sample_rows = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "표 스키마"
        verbose_name_plural = "표 스키마"

    def __str__(self) -> str:
        return self.table_name


class TableSearchRule(models.Model):
    name = models.CharField(max_length=100)
    table_name = models.CharField(max_length=100, blank=True)

    agg_hints_json = models.JSONField(default=dict, blank=True)
    column_synonyms_json = models.JSONField(default=dict, blank=True)
    numeric_hints_json = models.JSONField(default=list, blank=True)

    min_sim = models.FloatField(default=0.35)
    hard_filter_enabled = models.BooleanField(default=True)

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "표 검색 규칙"
        verbose_name_plural = "표 검색 규칙"
        ordering = ["-updated_at", "-id"]

    def __str__(self) -> str:
        target = self.table_name or "전체(전역)"
        return f"{self.name} / {target}"


class WebFeedback(Feedback):
    class Meta:
        proxy = True
        verbose_name = "웹 검색 피드백"
        verbose_name_plural = "웹 검색 피드백"
        app_label = "ragapp"


class RagSearchFeedback(Feedback):
    class Meta:
        proxy = True
        verbose_name = "RAG 검색 피드백"
        verbose_name_plural = "RAG 검색 피드백"
        app_label = "ragapp"


class FeedbackLog(models.Model):
    ANSWER_TYPE_CHOICES = [("web", "웹 검색"), ("rag", "RAG"), ("qa", "질문 챗봇")]

    answer_type = models.CharField(max_length=20, choices=ANSWER_TYPE_CHOICES)
    from_ui = models.CharField(max_length=50, blank=True)

    question = models.TextField(blank=True)
    answer = models.TextField(blank=True)
    sources = models.JSONField(default=list, blank=True)

    helpful = models.BooleanField(null=True, blank=True)
    reasons = models.JSONField(default=list, blank=True)
    comment = models.TextField(blank=True)
    stage = models.CharField(max_length=20, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "통합 피드백 로그"
        verbose_name_plural = "통합 피드백 로그"

    def short_question(self) -> str:
        if not self.question:
            return ""
        txt = self.question.strip().replace("\n", " ")
        return txt[:60] + ("…" if len(txt) > 60 else "")

    def short_answer(self) -> str:
        if not self.answer:
            return ""
        txt = self.answer.strip().replace("\n", " ")
        return txt[:60] + ("…" if len(txt) > 60 else "")

    def channel_label(self) -> str:
        return self.get_answer_type_display() or self.answer_type

    def __str__(self) -> str:
        return f"[{self.channel_label()}] {self.short_question() or '피드백'}"


class FeedbackReview(models.Model):
    STATUS_CHOICES = [("todo", "할 일"), ("doing", "처리 중"), ("done", "수정 완료"), ("wontfix", "보류 / 적용 안함")]

    feedback = models.OneToOneField(FeedbackLog, on_delete=models.CASCADE, related_name="review")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="todo")
    owner = models.CharField(max_length=50, blank=True)

    plan = models.TextField(blank=True)
    resolution = models.TextField(blank=True)

    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-feedback__created_at",)
        verbose_name = "피드백 리뷰"
        verbose_name_plural = "피드백 리뷰"

    def mark_resolved(self):
        if self.status in ("done", "wontfix") and not self.resolved_at:
            self.resolved_at = timezone.now()

    def __str__(self) -> str:
        return f"Review #{self.pk} for #{self.feedback_id}"


class AppLog(models.Model):
    LEVEL_CHOICES = [("DEBUG", "DEBUG"), ("INFO", "INFO"), ("WARN", "WARN"), ("ERROR", "ERROR")]

    created_at = models.DateTimeField(auto_now_add=True)
    level = models.CharField(max_length=10, choices=LEVEL_CHOICES, default="INFO")
    component = models.CharField(max_length=50, blank=True)
    trace_id = models.CharField(max_length=64, blank=True, db_index=True)

    message = models.TextField()
    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"[{self.created_at:%Y-%m-%d %H:%M:%S}] {self.level} {self.component}: {self.message[:50]}"


try:
    from .models_chat_retention import LiveChatMessage  # noqa: F401
except Exception:
    pass
