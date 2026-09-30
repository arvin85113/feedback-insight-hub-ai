from django.conf import settings
from django.db import models


class AuditLogImmutable(Exception):
    """Audit events are append-only."""


class NodeInstallation(models.Model):
    """Singleton describing this installation; the row always has pk=1."""

    setup_completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "節點安裝狀態"
        verbose_name_plural = "節點安裝狀態"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        installation, _created = cls.objects.get_or_create(pk=1)
        return installation

    @classmethod
    def setup_complete(cls):
        """Read-only check used on every request; never creates the row."""

        return cls.objects.filter(pk=1, setup_completed_at__isnull=False).exists()

    @property
    def is_setup_complete(self):
        return self.setup_completed_at is not None


class AuditEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise AuditLogImmutable("稽核紀錄不可修改")

    def delete(self):
        raise AuditLogImmutable("稽核紀錄不可刪除")


class NodeAuditEvent(models.Model):
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # Kept so the event still names the person after the account is removed.
    actor_email = models.CharField(max_length=254, blank=True)
    action = models.CharField(max_length=64)
    target = models.CharField(max_length=255, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = AuditEventQuerySet.as_manager()

    class Meta:
        verbose_name = "稽核紀錄"
        verbose_name_plural = "稽核紀錄"
        ordering = ["-created_at", "-pk"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AuditLogImmutable("稽核紀錄不可修改")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AuditLogImmutable("稽核紀錄不可刪除")
