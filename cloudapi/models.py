import hashlib
import secrets
import uuid

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
