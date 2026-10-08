"""seed_demo_beverage node paths against a real cloud subprocess (node-only analysis spec §4)."""

import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from cloudsync.models import CloudLink
from cloudsync.testing import CloudServer
from cloudsync.tests.utils import memory_keyring
from cloudsync.tokens import save_token
from feedback.management.commands.seed_demo_beverage import BEVERAGE_NODE_SURVEY_UUID
from feedback.models import KeywordCategory, Survey

CLOUD_SURVEY = """
from feedback.models import Survey
s = Survey.objects.get(uuid='{uuid}')
print(s.owner_node.name, s.published_version is not None)
"""


class NodeBeverageSeedTests(TestCase):
    def setUp(self):
        # Windows may keep the terminated server's SQLite handle briefly; leftover temp files are harmless.
        workdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(workdir.cleanup)
        self.cloud = CloudServer(Path(workdir.name), inbox=True, self_test_surveys=[BEVERAGE_NODE_SURVEY_UUID])
        self.cloud.__enter__()
        self.addCleanup(self.cloud.__exit__, None, None, None)
        keyring = memory_keyring()
        keyring.__enter__()
        self.addCleanup(keyring.__exit__, None, None, None)
        loopback = override_settings(CLOUD_SYNC_ALLOW_LOOPBACK_HTTP=True)
        loopback.enable()
        self.addCleanup(loopback.disable)
        CloudLink.relink(self.cloud.url, self.cloud.node_uuid)
        save_token(self.cloud.url, self.cloud.token)

    def node_create(self):
        call_command("seed_demo_beverage", "--node-create", stdout=StringIO())
        return Survey.objects.get(uuid=BEVERAGE_NODE_SURVEY_UUID)

    def test_node_create_publishes_owned_survey_with_keywords(self):
        survey = self.node_create()
        self.assertIsNotNone(survey.published_version)
        self.assertEqual([q.kind for q in survey.questions.order_by("order")],
                         ["single_choice", "single_choice", "multiple_choice", "scale", "scale", "single_choice",
                          "decimal", "decimal", "integer", "long_text"])
        self.assertEqual(KeywordCategory.objects.filter(survey=survey).count(), 6)
        cloud = self.cloud.shell(CLOUD_SURVEY.format(uuid=BEVERAGE_NODE_SURVEY_UUID)).strip().splitlines()[-1]
        self.assertEqual(cloud.split(), ["e2e", "True"])

    def test_node_create_requires_cloud_link(self):
        CloudLink.unlink()
        with self.assertRaisesMessage(CommandError, "尚未連結雲端"):
            call_command("seed_demo_beverage", "--node-create", stdout=StringIO())
        self.assertFalse(Survey.objects.filter(uuid=BEVERAGE_NODE_SURVEY_UUID).exists())
        self.assertFalse(KeywordCategory.objects.exists())

    def test_node_create_twice_is_a_no_op(self):
        self.node_create()
        survey = self.node_create()
        self.assertEqual(KeywordCategory.objects.filter(survey=survey).count(), 6)
        self.assertEqual(Survey.objects.filter(uuid=BEVERAGE_NODE_SURVEY_UUID).count(), 1)

    def test_beverage_through_inbox_end_to_end(self):
        from cloudsync.runner import run_cycle
        from feedback.management.commands.seed_demo_beverage import NAME_PREFIX
        from feedback.models import AnalysisJob, Answer, FeedbackSubmission

        survey = self.node_create()
        self.cloud._manage("seed_demo_beverage", "--inbox", "--count", "12", "--seed", "7")
        self.assertEqual(run_cycle(force=True), "ok")
        submissions = FeedbackSubmission.objects.filter(survey=survey)
        self.assertEqual(submissions.count(), 12)
        self.assertTrue(all(name.startswith(NAME_PREFIX) for name in submissions.values_list("respondent_name", flat=True)))
        self.assertTrue(Answer.objects.filter(submission__survey=survey, question__kind="multiple_choice")
                        .exclude(choice_codes=None).exists())
        self.assertTrue(AnalysisJob.objects.filter(survey=survey, status="pending").exists())
        statuses = self.cloud.shell(
            "from cloudapi.models import SubmissionReceipt; "
            "print(sorted(set(SubmissionReceipt.objects.values_list('status', flat=True))))"
        ).strip().splitlines()[-1]
        self.assertEqual(statuses, "['synced']")

    def test_node_create_resumes_after_a_failed_publish(self):
        from unittest.mock import patch

        from cloudapi.errors import PublishBlocked

        with patch("feedback.survey_lifecycle.commit", side_effect=PublishBlocked()):
            with self.assertRaises(CommandError):
                self.node_create()
        draft = Survey.objects.get(uuid=BEVERAGE_NODE_SURVEY_UUID)
        self.assertIsNone(draft.published_version)
        survey = self.node_create()
        self.assertIsNotNone(survey.published_version)
        self.assertEqual(KeywordCategory.objects.filter(survey=survey).count(), 6)
