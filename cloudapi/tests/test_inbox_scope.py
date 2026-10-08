"""The plaintext inbox accepts only server-allowed self-test surveys (node-only analysis spec §1)."""

import uuid

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from cloudapi.definition import add_question, blank_definition, serialize_definition
from cloudapi.errors import PublishBlocked
from cloudapi.inbox import SurveyClosed, accept_submission, uses_inbox
from cloudapi.models import InboxSubmission, NodeDevice, SubmissionReceipt
from cloudapi.writes import assign_survey_to_node, change_definition, create_node_survey
from config.settings import parse_uuid_list
from feedback.models import FeedbackSubmission, Question, Survey
from feedback.test_utils import cloud_only, published

User = get_user_model()


class ParseUuidListTests(SimpleTestCase):
    def test_settings_parse_comma_separated_uuids(self):
        self.assertEqual(parse_uuid_list(" A ,b,, "), frozenset({"a", "b"}))
        self.assertEqual(parse_uuid_list(""), frozenset())


@override_settings(CLOUD_INBOX_ENABLED=True, CLOUD_INBOX_REQUIRE_SELF_TEST=True,
                   CLOUD_INBOX_SELF_TEST_SURVEYS=frozenset())
class InboxScopeTests(TestCase):
    def setUp(self):
        self.node, _ = NodeDevice.issue("office")
        survey = Survey.objects.create(title="S", slug="s")
        self.question = Question.objects.create(survey=survey, title="Q", kind="short_text", data_type="text", order=1)
        self.survey = published(assign_survey_to_node(survey, self.node).survey)
        Survey.objects.filter(pk=self.survey.pk).update(inbox_since=timezone.now(), published_version=1,
                                                         analysis_definition_version=1)
        self.survey.refresh_from_db()
        self.customer = User.objects.create_user(username="c", password="x")

    def allowed(self, *surveys):
        return override_settings(CLOUD_INBOX_SELF_TEST_SURVEYS=frozenset(str(s.uuid) for s in surveys))

    def test_survey_outside_scope_does_not_use_inbox(self):
        self.assertFalse(uses_inbox(self.survey))
        with self.allowed(self.survey):
            self.assertTrue(uses_inbox(self.survey))

    def test_accept_outside_scope_raises_survey_closed(self):
        with self.assertRaises(SurveyClosed):
            accept_submission(self.survey, user=self.customer, submission_uuid=uuid.uuid4(), form_version=1,
                              consent_follow_up=False, answers={str(self.question.uuid): "好"})
        self.assertEqual((SubmissionReceipt.objects.count(), InboxSubmission.objects.count()), (0, 0))

    def node_draft(self):
        definition = blank_definition(uuid.uuid4(), title="節點草稿")
        add_question(definition, {"title": "感想", "kind": "long_text", "order": 1})
        revision, _ = create_node_survey(self.node, definition)
        return revision.survey

    def publish(self, survey):
        definition = serialize_definition(survey)
        definition["published"] = True
        change_definition(survey.uuid, expected_version=survey.definition_version, definition=definition)

    def test_publish_outside_scope_is_blocked(self):
        survey = self.node_draft()
        with self.assertRaises(PublishBlocked):
            self.publish(survey)
        survey.refresh_from_db()
        self.assertIsNone(survey.published_version)
        with self.allowed(survey):
            self.publish(survey)
        survey.refresh_from_db()
        self.assertIsNotNone(survey.published_version)

    @cloud_only
    def test_fill_page_refuses_node_survey_outside_scope(self):
        self.client.force_login(self.customer)
        response = self.client.post(
            reverse("feedback:survey-detail", args=[self.survey.slug]),
            {"definition_version": 1, f"question_{self.question.id}": "好",
             "meta-idempotency_key": "11111111-1111-1111-1111-111111111111"}, follow=True)
        self.assertContains(response, SurveyClosed.user_message)
        self.assertEqual((FeedbackSubmission.objects.count(), SubmissionReceipt.objects.count()), (0, 0))
