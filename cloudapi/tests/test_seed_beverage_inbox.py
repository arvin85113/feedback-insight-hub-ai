"""seed_demo_beverage --inbox: labelled simulations through the cloud inbox (node-only analysis spec §4)."""

import uuid
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from cloudapi.definition import serialize_definition
from cloudapi.inbox import accept_submission
from cloudapi.models import InboxSubmission, NodeDevice, SubmissionReceipt
from cloudapi.writes import change_definition, create_node_survey
from feedback.management.commands.seed_demo_beverage import (
    BEVERAGE_NODE_SURVEY_UUID,
    NAME_PREFIX,
    QUESTIONS,
    SURVEY_TITLE,
)
from feedback.models import FeedbackSubmission, Survey
from feedback.seed_support import survey_definition
from feedback.test_utils import cloud_only

ALLOWED = frozenset({str(BEVERAGE_NODE_SURVEY_UUID)})


@cloud_only
@override_settings(CLOUD_INBOX_ENABLED=True, CLOUD_INBOX_REQUIRE_SELF_TEST=True, CLOUD_INBOX_SELF_TEST_SURVEYS=ALLOWED)
class BeverageInboxSeedTests(TestCase):
    def setUp(self):
        node, _ = NodeDevice.issue("office")
        revision, _ = create_node_survey(node, survey_definition(
            survey_uuid=BEVERAGE_NODE_SURVEY_UUID, title=SURVEY_TITLE, questions=QUESTIONS))
        definition = serialize_definition(revision.survey)
        definition["published"] = True
        self.survey = change_definition(revision.survey.uuid, expected_version=revision.survey.definition_version,
                                        definition=definition).survey

    def inbox(self, *args):
        out = StringIO()
        call_command("seed_demo_beverage", "--inbox", *args, stdout=out)
        return out.getvalue()

    def test_inbox_writes_labelled_receipts_only(self):
        self.inbox("--count", "8", "--seed", "7")
        self.assertEqual(SubmissionReceipt.objects.count(), 8)
        names = [item.envelope["respondent"]["name"] for item in InboxSubmission.objects.all()]
        self.assertEqual(len(names), 8)
        self.assertTrue(all(name.startswith(NAME_PREFIX) for name in names))
        self.assertFalse(FeedbackSubmission.objects.exists())

    def test_inbox_rerun_with_same_seed_is_a_resend(self):
        self.inbox("--count", "8", "--seed", "7")
        output = self.inbox("--count", "8", "--seed", "7")
        self.assertEqual(SubmissionReceipt.objects.count(), 8)
        self.assertIn("重送 8", output)

    def test_inbox_refuses_survey_outside_self_test_scope(self):
        with override_settings(CLOUD_INBOX_SELF_TEST_SURVEYS=frozenset()):
            with self.assertRaises(CommandError):
                self.inbox("--count", "8", "--seed", "7")
        self.assertFalse(SubmissionReceipt.objects.exists())
        Survey.objects.filter(pk=self.survey.pk).update(uuid=uuid.uuid4())  # revisions protect the row from delete
        with self.assertRaises(CommandError):
            self.inbox("--count", "8", "--seed", "7")

    def test_inbox_requires_seed(self):
        with self.assertRaisesMessage(CommandError, "--seed"):
            self.inbox("--count", "8")

    def test_simulated_name_only_without_user(self):
        customer = get_user_model().objects.create_user(username="c", password="x")
        with self.assertRaises(ValueError):
            accept_submission(self.survey, user=customer, submission_uuid=uuid.uuid4(),
                              form_version=self.survey.published_version, consent_follow_up=False, answers={},
                              simulated_name="x")
