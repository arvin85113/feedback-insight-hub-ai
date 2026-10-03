import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from cloudapi.inbox import ResendRejected, accept_submission, quarantine_items
from cloudapi.models import InboxCounter, InboxSubmission, NodeDevice
from cloudapi.writes import assign_survey_to_node
from feedback.models import Question, Survey
from feedback.test_utils import cloud_only

User = get_user_model()


@cloud_only
@override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=True, CLOUD_INBOX_ENABLED=True)
class InboxManageTests(TestCase):
    def setUp(self):
        self.node, _ = NodeDevice.issue("office")
        survey = Survey.objects.create(title="S", slug="s")
        question = Question.objects.create(survey=survey, title="Q", kind="short_text", data_type="text", order=1)
        self.survey = assign_survey_to_node(survey, self.node).survey
        Survey.objects.filter(pk=self.survey.pk).update(inbox_since=timezone.now(), published_version=1, analysis_definition_version=1)
        self.customer = User.objects.create_user(username="c", password="x")
        self.answers = {str(question.uuid): "好吃的珍珠"}
        self.receipt = accept_submission(Survey.objects.get(pk=self.survey.pk), user=self.customer,
                                         submission_uuid=uuid.uuid4(), form_version=1, consent_follow_up=False,
                                         answers=self.answers).receipt
        quarantine_items(self.node, [{"submission_uuid": str(self.receipt.submission_uuid), "reason": "content_conflict"}])
        self.manager = User.objects.create_user(username="m", password="x", role=User.Role.MANAGER)
        self.client.force_login(self.manager)
        self.url = reverse("cloudapi-manage:inbox")

    def post(self, action):
        return self.client.post(self.url, {"action": action, "submission_uuid": str(self.receipt.submission_uuid)})

    def test_page_lists_quarantine_without_answer_text(self):
        page = self.client.get(self.url)
        self.assertContains(page, str(self.receipt.submission_uuid))
        self.assertContains(page, "content_conflict")
        self.assertNotContains(page, "好吃的珍珠")

    def test_requeue_keeps_occupancy(self):
        self.post("requeue")
        self.assertEqual(InboxSubmission.objects.get().state, "pending")
        self.receipt.refresh_from_db()
        self.assertEqual((self.receipt.status, self.receipt.resolution, self.receipt.resolved_by),
                         ("received", "requeued", self.manager))
        self.assertEqual(InboxCounter.objects.get(node=self.node).occupied_count, 1)

    def test_abandon_frees_once_and_blocks_resend(self):
        self.post("abandon")
        self.post("abandon")
        counter = InboxCounter.objects.get(node=self.node)
        self.assertEqual((counter.occupied_count, counter.occupied_bytes), (0, 0))
        self.assertFalse(InboxSubmission.objects.exists())
        self.receipt.refresh_from_db()
        self.assertEqual((self.receipt.status, self.receipt.resolution), ("abandoned", "abandoned"))
        with self.assertRaises(ResendRejected):
            accept_submission(Survey.objects.get(pk=self.survey.pk), user=self.customer,
                              submission_uuid=self.receipt.submission_uuid, form_version=1,
                              consent_follow_up=False, answers=self.answers)

    def test_customer_is_forbidden(self):
        self.client.force_login(self.customer)
        self.assertIn(self.client.get(self.url).status_code, (302, 403))
