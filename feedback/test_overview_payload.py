import uuid
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from cloudapi.models import NodeDevice, SubmissionReceipt
from cloudapi.writes import assign_survey_to_node

from .local_service import get_dashboard_payload
from .models import FeedbackSubmission, ImprovementUpdate, Survey
from .test_utils import cloud_only, published


class OverviewPayloadTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(username="m", password="x", role=User.Role.MANAGER)
        self.survey = published(Survey.objects.create(title="門市", slug="store"))

    def submit(self, survey, days_ago=0):
        submission = FeedbackSubmission.objects.create(survey=survey)
        FeedbackSubmission.objects.filter(pk=submission.pk).update(
            submitted_at=timezone.now() - timedelta(days=days_ago))

    def kinds(self, payload):
        return [item["kind"] for item in payload["attention_items"]]

    def test_trend_compares_with_the_previous_period(self):
        self.submit(self.survey, 0)
        self.submit(self.survey, 1)
        self.submit(self.survey, 8)
        payload = get_dashboard_payload(7)
        self.assertEqual(len(payload["trend"]), 7)
        self.assertEqual(payload["trend_total"], 2)
        self.assertTrue(payload["trend"][-1]["is_today"])
        self.assertEqual(payload["trend"][-1]["percent"], 100)
        recent = payload["kpis"][0]
        self.assertEqual((recent["value"], recent["change"]), (2, 100))

    def test_unknown_range_falls_back_to_seven_days(self):
        self.assertEqual(get_dashboard_payload(12)["range_days"], 7)
        self.assertEqual(len(get_dashboard_payload(30)["trend"]), 30)

    def test_no_change_is_shown_without_a_previous_period(self):
        self.submit(self.survey, 0)
        self.assertIsNone(get_dashboard_payload(7)["kpis"][0]["change"])

    def test_attention_lists_unnotified_improvement_first(self):
        Survey.objects.create(title="草稿", slug="draft")
        self.submit(published(Survey.objects.create(title="有回覆", slug="busy")))
        ImprovementUpdate.objects.create(
            survey=self.survey, title="加快出餐", status=ImprovementUpdate.Status.COMPLETED,
            created_by=self.manager, updated_by=self.manager)
        payload = get_dashboard_payload()
        # Draft surveys are not flagged; a live survey with no responses is; unanalysed responses are.
        self.assertEqual(self.kinds(payload), ["notify", "analysis", "share"])
        statuses = {row["slug"]: row["status"] for row in payload["survey_rows"]}
        self.assertEqual(statuses, {"store": "live", "busy": "live", "draft": "draft"})

    def test_archived_and_voided_are_left_out(self):
        Survey.objects.filter(pk=self.survey.pk).update(archived_at=timezone.now())
        other = published(Survey.objects.create(title="其他", slug="other"))
        FeedbackSubmission.objects.create(survey=other, voided_at=timezone.now())
        payload = get_dashboard_payload()
        self.assertEqual([row["slug"] for row in payload["survey_rows"]], ["other"])
        self.assertEqual(payload["survey_rows"][0]["response_total"], 0)

    def test_activity_groups_responses_per_survey_and_day(self):
        self.submit(self.survey, 0)
        self.submit(self.survey, 0)
        self.submit(self.survey, 3)
        titles = [entry["title"] for entry in get_dashboard_payload()["activity"]]
        self.assertEqual(titles, ["「門市」收到 2 份回覆", "「門市」收到 1 份回覆"])

    @cloud_only
    def test_node_survey_receipts_count_as_responses(self):
        node, _ = NodeDevice.issue("office")
        survey = published(assign_survey_to_node(Survey.objects.create(title="節點", slug="node"), node).survey)
        SubmissionReceipt.objects.create(
            submission_uuid=uuid.uuid4(), node=node, survey=survey, submitted_at=timezone.now(),
            definition_version=1, response_sequence=1, payload_hash="0" * 64,
            status=SubmissionReceipt.Status.RECEIVED)
        payload = get_dashboard_payload()
        row = next(row for row in payload["survey_rows"] if row["slug"] == "node")
        self.assertEqual((row["response_total"], row["recent_total"], row["unanalyzed"]), (1, 1, 0))
        self.assertEqual(payload["trend_total"], 1)
        # The node analyses its own surveys, so the cloud neither asks to share nor to analyse it.
        self.assertNotIn("節點", " ".join(item["title"] for item in payload["attention_items"]))

    def test_overview_page_renders_with_range(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("feedback:dashboard"), {"range": "30"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["range_days"], 30)
        self.assertContains(response, "近 30 天回覆")
        self.assertContains(response, 'href="?range=30" aria-current="true"')
