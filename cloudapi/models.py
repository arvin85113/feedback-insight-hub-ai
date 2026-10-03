import hashlib
import secrets
import uuid

from django.conf import settings
from django.db import models


class RevisionImmutable(Exception):
    """A survey definition revision never changes after it is written."""


class NodeDevice(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "啟用"
        REVOKED = "revoked", "已撤銷"

    TOKEN_PREFIX = "fih_"

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    name = models.CharField("名稱", max_length=120, unique=True)
    token_hash = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "本機節點裝置"
        verbose_name_plural = "本機節點裝置"

    def __str__(self):
        return self.name

    @staticmethod
    def hash_token(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @classmethod
    def _new_token(cls):
        return cls.TOKEN_PREFIX + secrets.token_urlsafe(32)

    @classmethod
    def issue(cls, name):
        token = cls._new_token()
        device = cls.objects.create(name=name, token_hash=cls.hash_token(token))
        return device, token

    def rotate(self):
        token = self._new_token()
        self.token_hash = self.hash_token(token)
        self.status = self.Status.ACTIVE
        self.save(update_fields=["token_hash", "status"])
        return token


class ChangeClock(models.Model):
    """Single row (pk=1) that hands out survey change sequence numbers in commit order."""

    value = models.PositiveBigIntegerField(default=0)
    pruned_through = models.PositiveBigIntegerField(default=0)


class SurveyDefinitionRevision(models.Model):
    survey = models.ForeignKey("feedback.Survey", on_delete=models.PROTECT, related_name="definition_revisions")
    version = models.PositiveIntegerField()
    definition = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("survey", "version"), name="cloudapi_revision_survey_version_uniq"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise RevisionImmutable("問卷版本建立後不可修改")
        super().save(*args, **kwargs)


class SurveyChange(models.Model):
    seq = models.PositiveBigIntegerField(unique=True)
    survey = models.ForeignKey("feedback.Survey", on_delete=models.PROTECT, related_name="+")
    definition_version = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["seq"]


class InboxSubmission(models.Model):
    """A customer reply waiting for its node (spec §3). The body is deleted once the node acknowledges it."""

    class State(models.TextChoices):
        PENDING = "pending", "待收"
        QUARANTINED = "quarantined", "隔離"

    submission_uuid = models.UUIDField(unique=True)
    node = models.ForeignKey(NodeDevice, on_delete=models.PROTECT, related_name="inbox_items")
    survey = models.ForeignKey("feedback.Survey", on_delete=models.PROTECT, related_name="+")
    envelope = models.JSONField()
    answers_hash = models.CharField(max_length=64)
    payload_hash = models.CharField(max_length=64)
    hash_version = models.PositiveSmallIntegerField(default=1)
    payload_version = models.PositiveSmallIntegerField(default=1)
    size_bytes = models.PositiveIntegerField()
    received_at = models.DateTimeField(auto_now_add=True)
    state = models.CharField(max_length=12, choices=State.choices, default=State.PENDING)
    quarantine_reason = models.CharField(max_length=32, blank=True)

    class Meta:
        indexes = [models.Index(fields=("node", "state", "received_at", "submission_uuid"), name="cloudapi_inbox_fetch_idx")]


class SubmissionReceipt(models.Model):
    """Long-lived record that a customer submitted; never holds answer text."""

    class Status(models.TextChoices):
        RECEIVED = "received", "已收件"
        SYNCED = "synced", "已同步"
        QUARANTINED = "quarantined", "衝突"
        ABANDONED = "abandoned", "已放棄"

    class Resolution(models.TextChoices):
        NONE = "", "無"
        UNRESOLVED = "unresolved", "待處理"
        REQUEUED = "requeued", "已放回"
        ABANDONED = "abandoned", "已放棄"

    submission_uuid = models.UUIDField(unique=True)
    node = models.ForeignKey(NodeDevice, on_delete=models.PROTECT, related_name="receipts")
    survey = models.ForeignKey("feedback.Survey", on_delete=models.PROTECT, related_name="receipts")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="survey_receipts"
    )
    submitted_at = models.DateTimeField()
    consent_follow_up = models.BooleanField(default=False)
    definition_version = models.PositiveIntegerField()
    response_sequence = models.PositiveBigIntegerField()
    payload_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.RECEIVED)
    synced_at = models.DateTimeField(null=True, blank=True)
    last_known_improvement_status = models.CharField(max_length=20, blank=True)
    quarantine_reason = models.CharField(max_length=32, blank=True)
    resolution = models.CharField(max_length=12, choices=Resolution.choices, default=Resolution.NONE, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )


class InboxCounter(models.Model):
    """Per-node occupancy of everything that still holds a body (pending and quarantined)."""

    node = models.OneToOneField(NodeDevice, on_delete=models.CASCADE, related_name="inbox_counter")
    occupied_count = models.PositiveBigIntegerField(default=0)
    occupied_bytes = models.PositiveBigIntegerField(default=0)

    @classmethod
    def for_node(cls, node):
        return cls.objects.get_or_create(node=node)[0]
