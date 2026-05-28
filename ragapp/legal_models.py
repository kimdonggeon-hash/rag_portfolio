# ragapp/legal_models.py
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone


class ConsentEvent(models.Model):
    """
    동의 수집 '증빙'용 최소 로그
    - client_key: 쿠키/UA/IP 기반 해시(개인식별 직접값 저장 X)
    - ip_hash: IP 원문 저장하지 않고 해시로만
    """

    client_key = models.CharField(max_length=64, db_index=True)
    ip_hash = models.CharField(max_length=64, blank=True, default="")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="consent_events",
    )

    consent_type = models.CharField(max_length=40, db_index=True, default="service")
    policy_version = models.CharField(max_length=40, db_index=True, default="v1")

    given_at = models.DateTimeField(default=timezone.now, db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    path = models.CharField(max_length=300, blank=True, default="")
    user_agent = models.CharField(max_length=500, blank=True, default="")

    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["client_key", "consent_type", "policy_version"]),
        ]

    def __str__(self) -> str:
        return f"ConsentEvent({self.consent_type}, {self.policy_version}, {self.given_at:%Y-%m-%d})"
