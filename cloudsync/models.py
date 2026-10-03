from django.db import models


class StaleLink(Exception):
    """The link was changed (re-linked or disconnected) while a sync was running."""


class CloudLink(models.Model):
    """Singleton (pk=1): where this node syncs to and how the last attempt went. The token lives in keyring.

    `generation` changes on every link or unlink. A running sync remembers the generation it
    started with and writes only through `update_if_current`, so it can never resurrect an old
    link or overwrite a new one's URL, node or cursor.
    """

    generation = models.PositiveIntegerField(default=0)
    api_url = models.URLField(blank=True)
    node_uuid = models.UUIDField(null=True, blank=True)
    cursor = models.CharField(max_length=80, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error_kind = models.CharField(max_length=20, blank=True)
    last_error_message = models.CharField(max_length=255, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]

    @property
    def is_linked(self):
        return bool(self.api_url and self.node_uuid)

    @classmethod
    def _reset(cls, **fields):
        cls.load()
        cls.objects.filter(pk=1).update(
            generation=models.F("generation") + 1,
            cursor="",
            last_error_kind="",
            last_error_message="",
            consecutive_failures=0,
            next_attempt_at=None,
            **fields,
        )
        return cls.load()

    @classmethod
    def relink(cls, api_url, node_uuid):
        return cls._reset(api_url=api_url, node_uuid=node_uuid)

    @classmethod
    def unlink(cls):
        return cls._reset(api_url="", node_uuid=None, last_success_at=None)

    @classmethod
    def update_if_current(cls, generation, **fields):
        return bool(cls.objects.filter(pk=1, generation=generation).update(**fields))
