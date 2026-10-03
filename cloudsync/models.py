import uuid

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
    # Last heartbeat's inbox figures (pending, capacity, oldest pending, deadline state).
    inbox_status = models.JSONField(default=dict, blank=True)

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


class SyncedSubmissionSource(models.Model):
    """Inbox provenance of a local reply: original JSON answers and hashes used for duplicate checks."""

    submission = models.OneToOneField(
        "feedback.FeedbackSubmission", on_delete=models.CASCADE, related_name="synced_source"
    )
    definition_version = models.PositiveIntegerField()
    definition_history = models.CharField(max_length=24)
    response_sequence = models.PositiveBigIntegerField()
    answers_hash = models.CharField(max_length=64)
    payload_hash = models.CharField(max_length=64)
    hash_version = models.PositiveSmallIntegerField()
    original_answers = models.JSONField()


class SurveySyncState(models.Model):
    """Reply watermark: every sequence up to it is written locally or abandoned in the cloud."""

    survey = models.OneToOneField("feedback.Survey", on_delete=models.CASCADE, related_name="sync_state")
    synced_through_sequence = models.PositiveBigIntegerField(default=0)
    abandoned_sequences = models.JSONField(default=list, blank=True)
    # Highest publish_sequence the cloud reported (heartbeat) and the highest this node handed out.
    cloud_publish_sequence = models.PositiveBigIntegerField(default=0)
    local_publish_sequence = models.PositiveBigIntegerField(default=0)

    @classmethod
    def advance(cls, survey):
        state, _ = cls.objects.select_for_update().get_or_create(survey=survey)
        done = set(
            SyncedSubmissionSource.objects.filter(
                submission__survey=survey, response_sequence__gt=state.synced_through_sequence
            ).values_list("response_sequence", flat=True)
        ) | set(state.abandoned_sequences)
        n = state.synced_through_sequence
        while n + 1 in done:
            n += 1
        state.synced_through_sequence = n
        state.abandoned_sequences = sorted(s for s in state.abandoned_sequences if s > n)
        state.save()
        return n


class PendingAck(models.Model):
    """A reply written locally whose ACK the cloud has not confirmed yet; resent until it is."""

    submission_uuid = models.UUIDField(unique=True)
    payload_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    last_status = models.CharField(max_length=16, blank=True)


class ResultUpload(models.Model):
    """One node publication with its identity and content frozen at creation; resends reuse both."""

    class Status(models.TextChoices):
        PENDING = "pending", "待上傳"
        UPLOADED = "uploaded", "已上傳"
        STALE = "stale", "過期"
        FAILED = "failed", "失敗"

    publish_uuid = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)
    survey = models.ForeignKey("feedback.Survey", on_delete=models.CASCADE, related_name="result_uploads")
    publish_sequence = models.PositiveBigIntegerField()
    content_hash = models.CharField(max_length=64)
    content = models.JSONField()
    published_at = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


def advance_and_schedule(survey):
    """Advance the reply watermark inside the caller's transaction and queue local analysis when it moves.

    Must run outside suppress_analysis_scheduling(), otherwise the schedule call is a no-op.
    """

    from feedback.analysis_jobs import schedule_survey_analysis

    before = SurveySyncState.objects.filter(survey=survey).values_list("synced_through_sequence", flat=True).first() or 0
    after = SurveySyncState.advance(survey)
    if after > before:
        schedule_survey_analysis(survey.pk, change="input")
    return after
