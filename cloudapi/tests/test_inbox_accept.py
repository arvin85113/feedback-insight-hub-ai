import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from cloudapi.definition import serialize_definition, update_survey
from cloudapi.inbox import DefinitionOutdated, InboxFull, ResendRejected, accept_submission
from cloudapi.models import InboxCounter, InboxSubmission, NodeDevice, SubmissionReceipt
from cloudapi.writes import assign_survey_to_node, change_definition
from feedback.models import FeedbackSubmission, Question, Survey

User = get_user_model()


@override_settings(CLOUD_INBOX_ENABLED=True)
class AcceptTests(TestCase):
    def setUp(self):
        self.node, _ = NodeDevice.issue("office")
        survey = Survey.objects.create(title="S", slug="s")
        self.question = Question.objects.create(survey=survey, title="Q", kind="short_text", data_type="text", order=1)
        self.survey = assign_survey_to_node(survey, self.node).survey  # version 1
        Survey.objects.filter(pk=self.survey.pk).update(inbox_since=timezone.now())
        self.survey.refresh_from_db()
        self.user = User.objects.create_user(username="c", password="x")
        self.answers = {str(self.question.uuid): "很好"}

    def accept(self, **overrides):
        kwargs = {"user": self.user, "submission_uuid": uuid.uuid4(), "form_version": 1,
                  "consent_follow_up": False, "answers": self.answers, **overrides}
        return accept_submission(self.survey, **kwargs)

    def test_accept_writes_inbox_receipt_sequence_flag_and_counter(self):
        result = self.accept()
        self.assertFalse(result.reused)
        self.assertEqual((result.receipt.status, result.receipt.response_sequence), ("received", 1))
        item = InboxSubmission.objects.get()
        self.assertEqual((item.state, item.envelope["answers"]), ("pending", self.answers))
        self.assertTrue(Question.objects.get(pk=self.question.pk).has_received_answer)
        counter = InboxCounter.objects.get(node=self.node)
        self.assertEqual((counter.occupied_count, counter.occupied_bytes), (1, item.size_bytes))
        self.assertFalse(FeedbackSubmission.objects.exists())

    def test_old_form_is_rejected_without_writing(self):
        definition = serialize_definition(self.survey)
        update_survey(definition, {"title": "新版"})
        change_definition(self.survey.uuid, expected_version=1, definition=definition)
        with self.assertRaises(DefinitionOutdated):
            self.accept()
        self.assertFalse(SubmissionReceipt.objects.exists())
        self.assertEqual(InboxCounter.for_node(self.node).occupied_count, 0)

    def test_resend_after_definition_update_returns_original(self):
        submission_uuid = uuid.uuid4()
        first = self.accept(submission_uuid=submission_uuid)
        definition = serialize_definition(Survey.objects.get(pk=self.survey.pk))
        update_survey(definition, {"title": "新版"})
        change_definition(self.survey.uuid, expected_version=1, definition=definition)
        again = self.accept(submission_uuid=submission_uuid)  # same content, form still says v1
        self.assertTrue(again.reused)
        self.assertEqual((again.receipt.pk, again.receipt.definition_version), (first.receipt.pk, 1))
        self.assertEqual(InboxCounter.for_node(self.node).occupied_count, 1)

    def test_same_uuid_with_other_content_user_or_abandoned_is_rejected(self):
        submission_uuid = uuid.uuid4()
        self.accept(submission_uuid=submission_uuid)
        other = User.objects.create_user(username="d", password="x")
        for overrides in ({"consent_follow_up": True}, {"user": other}):
            with self.subTest(overrides), self.assertRaises(ResendRejected):
                self.accept(submission_uuid=submission_uuid, **overrides)
        SubmissionReceipt.objects.update(status="abandoned")
        with self.assertRaises(ResendRejected):
            self.accept(submission_uuid=submission_uuid)

    @override_settings(CLOUD_INBOX_MAX_COUNT=1)
    def test_full_inbox_rejects_without_writing(self):
        self.accept()
        with self.assertRaises(InboxFull):
            self.accept()
        self.assertEqual((SubmissionReceipt.objects.count(), Survey.objects.get(pk=self.survey.pk).response_sequence), (1, 1))

    @override_settings(CLOUD_INBOX_MAX_ITEM_BYTES=10)
    def test_oversized_item_is_rejected(self):
        with self.assertRaises(InboxFull):
            self.accept()
