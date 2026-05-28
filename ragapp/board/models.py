# ragapp/board/models.py
from __future__ import annotations

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import models


class BoardCategory(models.Model):
    name = models.CharField(max_length=40)
    slug = models.SlugField(max_length=50, unique=True)
    order = models.PositiveIntegerField(default=0)
    is_notice = models.BooleanField(default=False)

    class Meta:
        ordering = ["order", "name"]

    def __str__(self) -> str:
        return self.name


class BoardPost(models.Model):
    category = models.ForeignKey(
        BoardCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name="posts"
    )

    title = models.CharField(max_length=200)
    body = models.TextField()

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="board_posts"
    )

    guest_name = models.CharField(max_length=20, blank=True, default="")
    guest_pw_hash = models.CharField(max_length=256, blank=True, default="")
    creator_fp = models.CharField(max_length=16, blank=True, default="", db_index=True)

    pinned = models.BooleanField(default=False)
    allow_comments = models.BooleanField(default=True)
    is_published = models.BooleanField(default=True)

    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    attachment = models.FileField(upload_to="board/%Y/%m/", null=True, blank=True)

    view_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_secret = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["is_published", "is_deleted", "-created_at"]),
            models.Index(fields=["category", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"[{self.pk}] {self.title}"

    @property
    def is_guest(self) -> bool:
        return self.author_id is None

    @property
    def is_notice(self) -> bool:
        try:
            return bool(self.category and self.category.is_notice)
        except Exception:
            return False

    def set_guest_password(self, raw: str) -> None:
        self.guest_pw_hash = make_password(raw)

    def check_guest_password(self, raw: str) -> bool:
        if not self.guest_pw_hash:
            return False
        return check_password(raw, self.guest_pw_hash)


class BoardComment(models.Model):
    post = models.ForeignKey(BoardPost, on_delete=models.CASCADE, related_name="comments")

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="board_comments"
    )

    guest_name = models.CharField(max_length=20, blank=True, default="")
    guest_pw_hash = models.CharField(max_length=256, blank=True, default="")
    creator_fp = models.CharField(max_length=16, blank=True, default="")

    body = models.TextField()

    is_hidden = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)
    hidden_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["post", "created_at"]),
            models.Index(fields=["is_hidden", "is_deleted", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"[{self.pk}] cmt on {self.post_id}"

    @property
    def is_guest(self) -> bool:
        return self.author_id is None

    def set_guest_password(self, raw: str) -> None:
        self.guest_pw_hash = make_password(raw)

    def check_guest_password(self, raw: str) -> bool:
        if not self.guest_pw_hash:
            return False
        return check_password(raw, self.guest_pw_hash)


class BoardReport(models.Model):
    class TargetType(models.TextChoices):
        POST = "post", "post"
        COMMENT = "comment", "comment"

    class Status(models.TextChoices):
        OPEN = "open", "open"
        RESOLVED = "resolved", "resolved"
        REJECTED = "rejected", "rejected"

    target_type = models.CharField(max_length=10, choices=TargetType.choices)
    post = models.ForeignKey(BoardPost, on_delete=models.CASCADE, null=True, blank=True, related_name="reports")
    comment = models.ForeignKey(BoardComment, on_delete=models.CASCADE, null=True, blank=True, related_name="reports")

    reason = models.CharField(max_length=30, default="spam")
    message = models.TextField(blank=True, default="")

    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="board_reports"
    )
    reporter_fp = models.CharField(max_length=16, blank=True, default="")

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="board_reports_handled"
    )
    handled_at = models.DateTimeField(null=True, blank=True)
    admin_note = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["target_type", "-created_at"]),
        ]

    def __str__(self) -> str:
        tgt = self.post_id or self.comment_id
        return f"[{self.pk}] {self.target_type}:{tgt} {self.status}"


class BoardAbuseKeyword(models.Model):
    """
    ✅ 금칙어/금칙 패턴을 운영자가 관리할 수 있게 DB로 둠
    - is_regex=False: 단어 포함 검사
    - is_regex=True : 정규식 검사
    """
    pattern = models.CharField(max_length=200)
    is_regex = models.BooleanField(default=False)
    enabled = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["enabled", "-created_at"])]

    def __str__(self) -> str:
        t = "REGEX" if self.is_regex else "WORD"
        return f"[{t}] {self.pattern}"

class BoardAdminActionLog(models.Model):
    class Action(models.TextChoices):
        BULK_RESOLVE = "bulk_resolve", "Bulk Resolve"
        BULK_REJECT = "bulk_reject", "Bulk Reject"
        REPORT_ACTION = "report_action", "Report Action"
        SYSTEM = "system", "System"

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="board_admin_logs",
    )
    actor_name = models.CharField(max_length=80, blank=True)

    action = models.CharField(max_length=32, choices=Action.choices, db_index=True)
    done = models.PositiveIntegerField(default=0)

    # 필터 스냅샷(검색/정렬 보조)
    status = models.CharField(max_length=16, blank=True)
    q = models.CharField(max_length=160, blank=True)
    auto_only = models.BooleanField(default=False)

    filters = models.JSONField(default=dict, blank=True)

    # ✅ “무슨 건 처리했는지” 추적용 (최대 20개)
    report_ids = models.JSONField(default=list, blank=True)
    report_ids_preview = models.CharField(max_length=300, blank=True)
    # ",12,33," 형태로 저장해서 포함검색 정확도↑
    report_ids_blob = models.CharField(max_length=340, blank=True, db_index=True)

    note = models.TextField(blank=True)

    path = models.CharField(max_length=200, blank=True)
    query_string = models.TextField(blank=True)
    ip = models.CharField(max_length=64, blank=True)
    ua = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at", "action"]),
            models.Index(fields=["actor_name"]),
        ]

    def save(self, *args, **kwargs):
        # actor_name 스냅샷
        if not self.actor_name and self.actor:
            try:
                self.actor_name = getattr(self.actor, "username", "") or "staff"
            except Exception:
                self.actor_name = "staff"

        # report_ids 정리(숫자만 + 최대 20개)
        ids = self.report_ids or []
        cleaned = []
        for x in ids:
            try:
                v = int(x)
                if v > 0:
                    cleaned.append(v)
            except Exception:
                continue
        cleaned = cleaned[:20]
        self.report_ids = cleaned

        self.report_ids_preview = ", ".join(str(v) for v in cleaned)
        self.report_ids_blob = ("," + ",".join(str(v) for v in cleaned) + ",") if cleaned else ""

        super().save(*args, **kwargs)

