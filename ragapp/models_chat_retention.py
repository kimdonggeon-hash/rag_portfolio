# ragapp/models_chat_retention.py
from __future__ import annotations

import re
from django.core.cache import cache
from datetime import timedelta
from django.conf import settings
from django.db import models
from django.utils import timezone


class RetentionClass(models.TextChoices):
    NORMAL = "normal", "일반(30일)"
    ABUSE = "abuse", "욕설/모욕(증빙보관)"
    LEGAL_HOLD = "legal_hold", "법무 홀드(수동)"


def retention_days_for(retention_class: str) -> int:
    if retention_class == RetentionClass.ABUSE:
        return int(getattr(settings, "ABUSE_RETENTION_DAYS", 365))
    if retention_class == RetentionClass.LEGAL_HOLD:
        return int(getattr(settings, "LEGAL_HOLD_RETENTION_DAYS", 1825))
    return int(getattr(settings, "CHAT_RETENTION_DAYS", 30))


def compute_purge_at(created_at, retention_class: str):
    return created_at + timedelta(days=retention_days_for(retention_class))


_ABUSE_CACHE_KEY = "livechat_abuse_patterns_v1"
_ABUSE_CACHE_TTL = 60  # 초 (너무 길게 잡지 말기)

def _load_abuse_patterns():
    """
    returns: list of (pattern:str, use_regex:bool, kind:str)
    """
    cached = cache.get(_ABUSE_CACHE_KEY)
    if cached is not None:
        return cached

    rows = []
    try:
        # DB 금지어
        rows.extend(
            list(
                LiveChatAbuseKeyword.objects.filter(is_active=True)
                .values_list("pattern", "use_regex", "kind")
            )
        )
    except Exception:
        # DB 조회 실패해도 서비스는 돌아가게(차단은 consumers에서 1차로 또 함)
        rows = []

    # (선택) settings fallback도 섞고 싶으면 유지
    for kw in (getattr(settings, "ABUSE_KEYWORDS", []) or []):
        if kw:
            rows.append((str(kw), False, "abuse"))

    cache.set(_ABUSE_CACHE_KEY, rows, _ABUSE_CACHE_TTL)
    return rows


def detect_abuse(text: str):
    """
    returns: (hit:bool, kind:str|None, matched_pattern:str|None)
    """
    t = (text or "").strip()
    if not t:
        return False, None, None

    low = t.lower()
    for pat, use_regex, kind in (_load_abuse_patterns() or []):
        if not pat:
            continue

        try:
            if use_regex:
                if re.search(pat, t, flags=re.IGNORECASE):
                    return True, (kind or "abuse"), pat
            else:
                if str(pat).lower() in low:
                    return True, (kind or "abuse"), str(pat)
        except re.error:
            # 잘못된 정규식은 무시
            continue
        except Exception:
            continue

    return False, None, None


def is_abusive_text(text: str) -> bool:
    hit, _, _ = detect_abuse(text)
    return hit



class LiveChatMessage(models.Model):
    """
    없으면 이걸 새로 쓰고,
    이미 메시지 테이블이 있으면 이 모델 대신 "필드들만" 기존 모델에 추가하면 됨.
    """
    session = models.ForeignKey("ragapp.LiveChatSession", on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=16, default="user")  # user/operator/system 등
    content = models.TextField()

    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    # ✅ 보관정책 핵심
    retention_class = models.CharField(
        max_length=16, choices=RetentionClass.choices, default=RetentionClass.NORMAL, db_index=True
    )
    purge_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # ✅ 플래그(자동/수동)
    flagged_at = models.DateTimeField(null=True, blank=True)
    flag_reason = models.CharField(max_length=200, blank=True, default="")
    flagged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="flagged_chat_messages"
    )

    def apply_retention(self, force: bool = False):
        if force or not self.purge_at:
            self.purge_at = compute_purge_at(self.created_at, self.retention_class)

    def save(self, *args, **kwargs):
        if self.retention_class == RetentionClass.NORMAL:
            hit, kind, pat = detect_abuse(self.content)
            if hit:
                self.retention_class = RetentionClass.ABUSE
                self.flagged_at = self.flagged_at or timezone.now()
                # kind/pattern 남겨두면 나중에 증빙/분석이 편함
                if not self.flag_reason:
                    k = kind or "abuse"
                    p = (pat or "match")[:80]
                    self.flag_reason = f"auto:{k}:{p}"

        self.apply_retention(force=False)
        super().save(*args, **kwargs)

    class Meta:
        indexes = [
            models.Index(fields=["retention_class", "purge_at"]),
            models.Index(fields=["session", "created_at"]),
        ]


class ChatEvidence(models.Model):
    """
    욕설/모욕 등 “증빙 보관”용.
    원본 메시지가 파기되어도 증빙은 남길 수 있게 별도로 snapshot 저장.
    """
    session = models.ForeignKey("ragapp.LiveChatSession", on_delete=models.CASCADE, related_name="evidences")
    message = models.ForeignKey("ragapp.LiveChatMessage", null=True, blank=True, on_delete=models.SET_NULL)

    captured_text = models.TextField()
    reason = models.CharField(max_length=200, blank=True, default="")

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    purge_at = models.DateTimeField(null=True, blank=True, db_index=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="created_chat_evidences"
    )

    def save(self, *args, **kwargs):
        if not self.purge_at:
            # 증빙은 ABUSE 기준으로 보관기간 적용
            self.purge_at = self.created_at + timedelta(days=retention_days_for(RetentionClass.ABUSE))
        super().save(*args, **kwargs)

    class Meta:
        indexes = [models.Index(fields=["purge_at"])]

class PurgeRun(models.Model):
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, default="running")  # running/success/error
    messages_deleted = models.IntegerField(default=0)
    evidences_deleted = models.IntegerField(default=0)
    note = models.TextField(blank=True, default="")

class LiveChatAbuseKeyword(models.Model):
    KIND_CHOICES = [
        ("abuse", "욕설/모욕"),
        ("sexual", "성희롱"),
    ]

    kind = models.CharField(
        "분류",
        max_length=16,
        choices=KIND_CHOICES,
        default="abuse",
        db_index=True,
    )

    pattern = models.CharField(
        "금지어 (키워드 또는 정규식)",
        max_length=128,
        unique=True,
        help_text="예: 씨발 / fuck / (바보|멍청이)",
    )
    use_regex = models.BooleanField(
        "정규식 사용",
        default=False,
        help_text="체크하면 pattern을 정규식으로 해석합니다.",
    )
    is_active = models.BooleanField(
        "사용 여부",
        default=True,
        db_index=True,
    )
    note = models.CharField(
        "메모",
        max_length=200,
        blank=True,
    )
    created_at = models.DateTimeField("등록 시각", auto_now_add=True)
    updated_at = models.DateTimeField("수정 시각", auto_now=True)

    class Meta:
        verbose_name = "상담 욕설/금지어"
        verbose_name_plural = "상담 욕설/금지어 목록"

    def __str__(self) -> str:
        return f"[{self.kind}] {self.pattern}"

# ✅ 추가: 기존 코드 호환용 alias 모델 (DB 테이블 추가 없음)
class LiveChatBlockPattern(LiveChatAbuseKeyword):
    """
    호환용 이름.
    기존 코드에서 LiveChatBlockPattern을 import해도 깨지지 않게 유지.
    실제 데이터는 LiveChatAbuseKeyword 테이블을 그대로 사용.
    """
    class Meta:
        proxy = True
        verbose_name = "상담 차단 패턴"
        verbose_name_plural = "상담 차단 패턴 목록"