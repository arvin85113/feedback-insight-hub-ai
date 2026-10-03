import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings

from cloudsync.client import TRANSIENT, CloudClient, CloudError
from cloudsync.models import CloudLink, PendingAck, SurveySyncState, SyncedSubmissionSource
from cloudsync.runner import run_cycle
from cloudsync.testing import CloudServer
from cloudsync.tests.utils import memory_keyring
from cloudsync.tokens import save_token
from feedback.models import AnalysisJob, Answer, FeedbackSubmission, Survey

SEED = """
from django.contrib.auth import get_user_model
from django.utils import timezone
from cloudapi.models import NodeDevice
from cloudapi.writes import assign_survey_to_node
from feedback.models import Question, Survey
survey = Survey.objects.create(title="門市問卷", slug="{slug}")
Question.objects.create(survey=survey, title="感想", kind="long_text", data_type="text", order=1)
survey = assign_survey_to_node(survey, NodeDevice.objects.get(name="e2e")).survey
Survey.objects.filter(pk=survey.pk).update(inbox_since=timezone.now())
get_user_model().objects.get_or_create(username="{slug}-customer")
print(survey.uuid)
"""

SUBMIT = """
import uuid
from django.contrib.auth import get_user_model
from cloudapi.inbox import accept_submission
from feedback.models import Survey
s = Survey.objects.get(uuid='{survey}')
q = s.questions.get(title='感想')
r = accept_submission(s, user=get_user_model().objects.get(username='{slug}-customer'), submission_uuid=uuid.UUID('{sub}'),
                      form_version=s.definition_version, consent_follow_up=True, answers={{str(q.uuid): '{text}'}})
print(r.receipt.response_sequence)
"""

COUNTER = """
from cloudapi.models import InboxCounter, NodeDevice
print(InboxCounter.for_node(NodeDevice.objects.get(name="e2e")).occupied_count)
"""

CLOUD_STATE = """
from cloudapi.models import InboxCounter, InboxSubmission, SubmissionReceipt
r = SubmissionReceipt.objects.get(submission_uuid='{sub}')
print(r.status, r.quarantine_reason or '-', InboxSubmission.objects.filter(submission_uuid='{sub}').count(),
      InboxCounter.objects.get(node=r.node).occupied_count)
"""


class InboxEndToEndTests(TestCase):
    """One cloud subprocess (inbox enabled) shared by all tests; each test seeds its own survey."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Windows may keep the terminated server's SQLite handle briefly; leftover temp files are harmless.
        cls._workdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.cloud = CloudServer(Path(cls._workdir.name), inbox=True)
        cls.cloud.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.cloud.__exit__(None, None, None)
        cls._workdir.cleanup()
        super().tearDownClass()

    def setUp(self):
        self._keyring = memory_keyring()
        self._keyring.__enter__()
        self.addCleanup(self._keyring.__exit__, None, None, None)
        loopback = override_settings(CLOUD_SYNC_ALLOW_LOOPBACK_HTTP=True)
        loopback.enable()
        self.addCleanup(loopback.disable)
        CloudLink.relink(self.cloud.url, self.cloud.node_uuid)
        save_token(self.cloud.url, self.cloud.token)
        self.slug = self._testMethodName.replace("_", "-")[:40]
        self.survey_uuid = self.cloud.shell(SEED.replace("{slug}", self.slug)).strip().splitlines()[-1]
        self.sub = str(uuid.uuid4())

    def submit(self, text="很好"):
        return self.cloud.shell(SUBMIT.format(survey=self.survey_uuid, slug=self.slug, sub=self.sub, text=text))

    def cloud_counter(self):
        # The counter is per node and the cloud is shared by all tests in this class.
        return int(self.cloud.shell(COUNTER).strip().splitlines()[-1])

    def cloud_state(self):
        return self.cloud.shell(CLOUD_STATE.format(sub=self.sub)).strip().splitlines()[-1].split()

    def test_submit_sync_ack_and_counts(self):
        before = self.cloud_counter()
        self.submit()
        self.assertEqual(run_cycle(force=True), "ok")
        local = FeedbackSubmission.objects.get(idempotency_key=self.sub)
        self.assertTrue(local.consent_follow_up)
        self.assertIsNone(local.user)
        self.assertEqual(Answer.objects.get(submission=local).value, "很好")
        self.assertEqual(self.cloud_state(), ["synced", "-", "0", str(before)])
        survey = Survey.objects.get(uuid=self.survey_uuid)
        self.assertEqual(SurveySyncState.objects.get(survey=survey).synced_through_sequence, 1)
        self.assertTrue(AnalysisJob.objects.filter(survey=survey, status="pending").exists())
        self.assertFalse(PendingAck.objects.exists())

    def test_crash_after_write_before_ack_does_not_duplicate(self):
        before = self.cloud_counter()
        self.submit()
        original_post = CloudClient.post
        calls = {"ack": 0}

        def flaky_post(client, path, body=None):
            if path == "inbox/ack/":
                calls["ack"] += 1
                if calls["ack"] == 1:
                    raise CloudError(TRANSIENT, "connection dropped")
            return original_post(client, path, body)

        with patch.object(CloudClient, "post", flaky_post):
            self.assertEqual(run_cycle(force=True), TRANSIENT)
            self.assertTrue(PendingAck.objects.filter(submission_uuid=self.sub).exists())
            self.assertEqual(self.cloud_state()[:3], ["received", "-", "1"])
            self.assertEqual(run_cycle(force=True), "ok")
        self.assertEqual(FeedbackSubmission.objects.filter(idempotency_key=self.sub).count(), 1)
        self.assertFalse(PendingAck.objects.exists())
        self.assertEqual(self.cloud_state(), ["synced", "-", "0", str(before)])

    def test_conflicting_content_is_quarantined_on_both_sides(self):
        self.assertEqual(run_cycle(force=True), "ok")  # brings the survey definition down
        survey = Survey.objects.get(uuid=self.survey_uuid)
        local = FeedbackSubmission.objects.create(survey=survey, idempotency_key=self.sub)
        SyncedSubmissionSource.objects.create(
            submission=local, definition_version=1, definition_history="recorded", response_sequence=1,
            answers_hash="0" * 64, payload_hash="0" * 64, hash_version=1, original_answers={},
        )
        self.submit()
        run_cycle(force=True)
        self.assertEqual(self.cloud_state()[:3], ["quarantined", "content_conflict", "1"])
        self.assertFalse(Answer.objects.filter(submission=local).exists())
