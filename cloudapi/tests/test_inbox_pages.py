from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from cloudapi.definition import serialize_definition, update_survey
from cloudapi.models import InboxSubmission, NodeDevice, SubmissionReceipt
from cloudapi.receipts import received_counts
from cloudapi.writes import assign_survey_to_node, change_definition
from feedback.models import FeedbackSubmission, Question, Survey
from feedback.test_utils import cloud_only, published

User = get_user_model()


@cloud_only
@override_settings(CLOUD_INBOX_ENABLED=True)
class InboxFillPageTests(TestCase):
    def setUp(self):
        node, _ = NodeDevice.issue("office")
        survey = Survey.objects.create(title="S", slug="s")
        self.question = Question.objects.create(survey=survey, title="Q", kind="short_text", data_type="text", order=1)
        self.survey = published(assign_survey_to_node(survey, node).survey)
        Survey.objects.filter(pk=self.survey.pk).update(inbox_since=timezone.now(), published_version=1, analysis_definition_version=1)
        self.customer = User.objects.create_user(username="c", password="x")
        self.client.force_login(self.customer)
        self.url = reverse("feedback:survey-detail", args=["s"])

    def submit(self, version=1, **extra):
        return self.client.post(self.url, {"definition_version": version, f"question_{self.question.id}": "好",
                                           "meta-idempotency_key": extra.pop("key", "11111111-1111-1111-1111-111111111111"),
                                           **extra}, follow=True)

    def test_form_carries_version_and_submit_goes_to_inbox(self):
        self.assertContains(self.client.get(self.url), 'name="definition_version" value="1"')
        self.submit()
        self.assertEqual(InboxSubmission.objects.count(), 1)
        self.assertFalse(FeedbackSubmission.objects.exists())
        self.assertContains(self.client.get(self.url), "你已填答過這份問卷。")

    def test_outdated_form_shows_message_and_keeps_input(self):
        response = self.submit(version=99)
        self.assertContains(response, "問卷已更新，請確認後重新送出")
        self.assertContains(response, 'value="好"')
        self.assertFalse(SubmissionReceipt.objects.exists())

    @override_settings(CLOUD_INBOX_MAX_COUNT=0)
    def test_full_inbox_message(self):
        self.assertContains(self.submit(), "目前暫停收件，請稍後再試")

    def test_receipt_appears_in_customer_home_and_counts(self):
        self.submit()
        home = self.client.get(reverse("feedback:customer-home"))
        self.assertEqual(home.context["submission_count"], 1)
        self.assertEqual(received_counts([self.survey.pk]), {self.survey.pk: 1})

    @override_settings(CLOUD_INBOX_ENABLED=False)
    def test_switch_off_keeps_the_old_flow(self):
        self.submit()
        self.assertEqual((FeedbackSubmission.objects.count(), InboxSubmission.objects.count()), (1, 0))
