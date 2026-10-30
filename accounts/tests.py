from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from feedback.test_utils import cloud_only


@cloud_only
class SharedLoginEntryTests(TestCase):
    """Managers and customers share one login page; routing follows the account's role."""

    def setUp(self):
        User = get_user_model()
        self.manager = User.objects.create_user(username="entry-manager", password="pass-12345", role="manager")
        self.customer = User.objects.create_user(
            username="entry-customer", password="pass-12345", role="customer", is_email_verified=True
        )
        self.login_url = reverse("accounts:login")

    def login(self, username, next_url=None):
        data = {"username": username, "password": "pass-12345"}
        if next_url:
            data["next"] = next_url
        return self.client.post(self.login_url, data)

    def test_customer_following_the_manager_entry_lands_on_customer_home(self):
        response = self.login("entry-customer", reverse("feedback:dashboard"))
        self.assertRedirects(response, reverse("feedback:customer-home"))

    def test_manager_following_the_customer_entry_lands_on_dashboard(self):
        response = self.login("entry-manager", reverse("feedback:customer-home"))
        self.assertRedirects(response, reverse("feedback:dashboard"))

    def test_matching_entry_and_other_pages_keep_their_next_target(self):
        stats = reverse("feedback:stats-overview")
        self.assertRedirects(self.login("entry-manager", stats), stats)
        self.client.logout()
        notifications = reverse("feedback:customer-notifications")
        self.assertRedirects(self.login("entry-customer", notifications), notifications)

    def test_no_next_routes_by_role(self):
        self.assertRedirects(self.login("entry-manager"), reverse("feedback:dashboard"))
        self.client.logout()
        self.assertRedirects(self.login("entry-customer"), reverse("feedback:customer-home"))

    def test_login_page_is_role_neutral_and_marks_the_chosen_entry(self):
        neutral = self.client.get(self.login_url)
        self.assertContains(neutral, "管理者／顧客共用")
        self.assertNotContains(neutral, "is-intended")
        self.assertNotContains(neutral, ">Workspace<")

        manager_entry = self.client.get(self.login_url, {"next": reverse("feedback:dashboard")})
        self.assertEqual(manager_entry.context["entry_role"], "manager")
        self.assertContains(manager_entry, "你正要前往", count=1)

        customer_entry = self.client.get(self.login_url, {"next": reverse("feedback:customer-home")})
        self.assertEqual(customer_entry.context["entry_role"], "customer")


@cloud_only
class NotificationSettingsListTests(TestCase):
    """The per-survey list on the notification settings page covers inbox-only (node flow) replies too."""

    def setUp(self):
        import uuid

        from django.utils import timezone

        from cloudapi.models import NodeDevice, SubmissionReceipt
        from cloudapi.writes import assign_survey_to_node
        from feedback.models import FeedbackSubmission, Survey

        User = get_user_model()
        self.customer = User.objects.create_user(username="pref-customer", password="x", role="customer")
        node, _ = NodeDevice.issue("pref-node")
        self.inbox_survey = assign_survey_to_node(Survey.objects.create(title="收件匣問卷", slug="inbox"), node).survey
        SubmissionReceipt.objects.create(
            submission_uuid=uuid.uuid4(), node=node, survey=self.inbox_survey, submitted_at=timezone.now(),
            definition_version=1, response_sequence=1, payload_hash="0" * 64, status="received",
            user=self.customer, consent_follow_up=True)
        self.local_survey = Survey.objects.create(title="一般問卷", slug="local")
        FeedbackSubmission.objects.create(survey=self.local_survey, user=self.customer, consent_follow_up=False)
        self.client.force_login(self.customer)

    def test_inbox_only_survey_is_listed_read_only(self):
        html = self.client.get(reverse("accounts:preferences")).content.decode()
        self.assertIn("收件匣問卷", html)
        self.assertIn("這份問卷的通知在填答時就已決定", html)
        self.assertIn("會通知", html)
        self.assertNotIn(f'id="survey-toggle-{self.inbox_survey.id}"', html)
        self.assertIn("一般問卷", html)
        self.assertIn(f'id="survey-toggle-{self.local_survey.id}"', html)
