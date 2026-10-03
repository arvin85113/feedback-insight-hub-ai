from datetime import datetime, timezone as dt_timezone

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from cloudapi.models import InboxCounter, NodeDevice
from cloudapi.writes import assign_survey_to_node
from feedback.models import FeedbackSubmission, Survey


class SchemaTests(TestCase):
    def test_submitted_at_can_be_written_and_defaults_to_now(self):
        survey = Survey.objects.create(title="S", slug="s")
        original = datetime(2026, 1, 2, 3, 4, tzinfo=dt_timezone.utc)
        kept = FeedbackSubmission.objects.create(survey=survey, submitted_at=original, respondent_ref="ref")
        kept.refresh_from_db()
        self.assertEqual((kept.submitted_at, kept.respondent_ref), (original, "ref"))
        self.assertIsNotNone(FeedbackSubmission.objects.create(survey=survey).submitted_at)

    def test_counter_is_created_lazily_per_node(self):
        node, _ = NodeDevice.issue("office")
        self.assertEqual((InboxCounter.for_node(node).occupied_count, InboxCounter.objects.count()), (0, 1))


class EnableInboxCommandTests(TestCase):
    """The inbox now starts when a node-owned survey is published (builder spec §4.3)."""

    @override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=True, CLOUD_INBOX_ENABLED=True)
    def test_command_is_retired(self):
        node, _ = NodeDevice.issue("office")
        assign_survey_to_node(Survey.objects.create(title="S", slug="s"), node)
        with self.assertRaisesMessage(CommandError, "已改為發布時設定收件匣，請改用發布"):
            call_command("enable_survey_inbox", survey="s")
        self.assertIsNone(Survey.objects.get(slug="s").inbox_since)
