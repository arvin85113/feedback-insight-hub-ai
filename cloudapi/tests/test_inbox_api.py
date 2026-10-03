import json
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from cloudapi.inbox import accept_submission
from cloudapi.models import InboxCounter, InboxSubmission, NodeDevice, SubmissionReceipt
from cloudapi.writes import assign_survey_to_node
from feedback.models import Question, Survey
from feedback.test_utils import cloud_only

BASE = "/api/node/v1/"


@cloud_only
@override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=True, CLOUD_INBOX_ENABLED=True)
class InboxApiTests(TestCase):
    def setUp(self):
        self.node, self.token = NodeDevice.issue("office")
        self.other, self.other_token = NodeDevice.issue("other")
        survey = Survey.objects.create(title="S", slug="s")
        question = Question.objects.create(survey=survey, title="Q", kind="short_text", data_type="text", order=1)
        self.survey = assign_survey_to_node(survey, self.node).survey
        Survey.objects.filter(pk=self.survey.pk).update(inbox_since=timezone.now())
        user = get_user_model().objects.create_user(username="c", password="x")
        self.receipt = accept_submission(Survey.objects.get(pk=self.survey.pk), user=user, submission_uuid=uuid.uuid4(),
                                         form_version=1, consent_follow_up=False,
                                         answers={str(question.uuid): "好"}).receipt

    def call(self, method, path, body=None, token=None):
        kwargs = {"content_type": "application/json", "data": json.dumps(body)} if body is not None else {}
        return getattr(self.client, method)(BASE + path, HTTP_AUTHORIZATION=f"Bearer {token or self.token}", **kwargs)

    def ack(self, payload_hash=None, token=None):
        item = {"submission_uuid": str(self.receipt.submission_uuid), "payload_hash": payload_hash or self.receipt.payload_hash}
        return self.call("post", "inbox/ack/", {"items": [item]}, token=token).json()["results"][0]["status"]

    def test_list_is_scoped_to_the_node(self):
        self.assertEqual(len(self.call("get", "inbox/").json()["items"]), 1)
        self.assertEqual(self.call("get", "inbox/", token=self.other_token).json()["items"], [])

    def test_ack_then_repeat_then_wrong_hash(self):
        self.assertEqual(self.ack(), "acked")
        self.assertEqual(self.ack(), "already_acked")
        self.assertEqual(self.ack(payload_hash="0" * 64), "conflict")
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.status, "synced")  # not downgraded
        counter = InboxCounter.objects.get(node=self.node)
        self.assertEqual((counter.occupied_count, counter.occupied_bytes), (0, 0))
        self.assertFalse(InboxSubmission.objects.exists())

    def test_other_node_gets_not_found(self):
        self.assertEqual(self.ack(token=self.other_token), "not_found")
        self.assertTrue(InboxSubmission.objects.exists())

    def test_quarantined_items_leave_the_batch_and_keep_occupancy(self):
        body = {"items": [{"submission_uuid": str(self.receipt.submission_uuid), "reason": "content_conflict"}]}
        self.assertEqual(self.call("post", "inbox/quarantine/", body).json()["results"][0]["status"], "quarantined")
        self.assertEqual(self.call("get", "inbox/").json()["items"], [])
        self.assertEqual(self.ack(), "not_found")
        self.assertEqual(InboxCounter.objects.get(node=self.node).occupied_count, 1)
        self.assertEqual(self.call("post", "inbox/quarantine/", {"items": [{"submission_uuid": str(uuid.uuid4()),
                                                                             "reason": "bogus"}]}).status_code, 400)

    def test_heartbeat_reports_inbox_and_survey_sequences(self):
        body = self.call("post", "heartbeat/", {}).json()
        self.assertEqual((body["inbox"]["pending_count"], body["inbox"]["deadline_state"]), (1, "ok"))
        self.assertEqual(body["surveys"], [{"survey_uuid": str(self.survey.uuid), "response_sequence": 1,
                                            "abandoned_sequences": [], "publish_sequence": 0}])


@cloud_only
@override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=True, CLOUD_INBOX_ENABLED=True)
class InboxLockOrderTests(InboxApiTests):
    """Spec §7.4: every inbox writer touches the receipt before the body (receipt, body, counter)."""

    def first_table(self, action):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        receipts, bodies = SubmissionReceipt._meta.db_table, InboxSubmission._meta.db_table
        with CaptureQueriesContext(connection) as queries:
            action()
        for query in queries.captured_queries:
            sql = query["sql"]
            if receipts in sql or bodies in sql:
                return receipts if receipts in sql else bodies
        return None

    def test_ack_touches_receipt_before_body(self):
        from cloudapi.inbox import ack_items

        item = {"submission_uuid": str(self.receipt.submission_uuid), "payload_hash": self.receipt.payload_hash}
        self.assertEqual(self.first_table(lambda: ack_items(self.node, [item])), SubmissionReceipt._meta.db_table)

    def test_quarantine_touches_receipt_before_body(self):
        from cloudapi.inbox import quarantine_items

        item = {"submission_uuid": str(self.receipt.submission_uuid), "reason": "content_conflict"}
        self.assertEqual(self.first_table(lambda: quarantine_items(self.node, [item])), SubmissionReceipt._meta.db_table)
