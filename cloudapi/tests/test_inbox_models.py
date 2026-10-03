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
    def setUp(self):
        node, _ = NodeDevice.issue("office")
        assign_survey_to_node(Survey.objects.create(title="S", slug="s"), node)

    def test_refuses_when_either_switch_is_off(self):
        for switches in ({"CLOUD_SYNC_PROTOTYPE_ENABLED": False, "CLOUD_INBOX_ENABLED": True},
                         {"CLOUD_SYNC_PROTOTYPE_ENABLED": True, "CLOUD_INBOX_ENABLED": False}):
            with self.subTest(switches), override_settings(**switches), self.assertRaises(CommandError):
                call_command("enable_survey_inbox", survey="s")
        self.assertIsNone(Survey.objects.get(slug="s").inbox_since)

    @override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=True, CLOUD_INBOX_ENABLED=True)
    def test_sets_inbox_since_once(self):
        call_command("enable_survey_inbox", survey="s")
        first = Survey.objects.get(slug="s").inbox_since
        call_command("enable_survey_inbox", survey="s")
        self.assertEqual(Survey.objects.get(slug="s").inbox_since, first)

    @override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=True, CLOUD_INBOX_ENABLED=True)
    def test_requires_owner_node(self):
        Survey.objects.create(title="P", slug="p")
        with self.assertRaises(CommandError):
            call_command("enable_survey_inbox", survey="p")
