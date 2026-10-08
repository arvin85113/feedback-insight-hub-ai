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


@override_settings(CLOUD_INBOX_ENABLED=True, CLOUD_INBOX_REQUIRE_SELF_TEST=True)
class SelfTestSurveyAccessTests(TestCase):
    """Final review: real customers stay out of the plaintext inbox; blocked publishes say why."""

    def setUp(self):
        self.node, _ = NodeDevice.issue("office")
        survey = Survey.objects.create(title="S", slug="s")
        self.question = Question.objects.create(survey=survey, title="Q", kind="short_text", data_type="text", order=1)
        self.survey = published(assign_survey_to_node(survey, self.node).survey)
        Survey.objects.filter(pk=self.survey.pk).update(inbox_since=timezone.now(), published_version=1,
                                                         analysis_definition_version=1)
        self.survey.refresh_from_db()
        self.scope = override_settings(CLOUD_INBOX_SELF_TEST_SURVEYS=frozenset({str(self.survey.uuid)}))
        self.scope.enable()
        self.addCleanup(self.scope.disable)

    def accept(self, user):
        return accept_submission(self.survey, user=user, submission_uuid=uuid.uuid4(), form_version=1,
                                 consent_follow_up=False, answers={str(self.question.uuid): "好"})

    def test_customer_reply_to_self_test_survey_is_refused(self):
        customer = User.objects.create_user(username="c", password="x")
        with self.assertRaises(SurveyClosed):
            self.accept(customer)
        self.assertFalse(SubmissionReceipt.objects.exists())

    def test_manager_and_seeded_replies_are_accepted(self):
        manager = User.objects.create_user(username="m", password="x", role="manager")
        self.accept(manager)
        self.accept(None)
        self.assertEqual(SubmissionReceipt.objects.count(), 2)

    @cloud_only
    def test_customer_fill_page_is_refused_without_writing(self):
        customer = User.objects.create_user(username="c", password="x")
        self.client.force_login(customer)
        response = self.client.post(
            reverse("feedback:survey-detail", args=[self.survey.slug]),
            {"definition_version": 1, f"question_{self.question.id}": "好",
             "meta-idempotency_key": "11111111-1111-1111-1111-111111111111"}, follow=True)
        self.assertContains(response, SurveyClosed.user_message)
        self.assertFalse(SubmissionReceipt.objects.exists())

    def test_publish_outside_scope_explains_the_self_test_limit(self):
        definition = blank_definition(uuid.uuid4(), title="節點草稿")
        add_question(definition, {"title": "感想", "kind": "long_text", "order": 1})
        draft = create_node_survey(self.node, definition)[0].survey
        published_definition = serialize_definition(draft)
        published_definition["published"] = True
        with self.assertRaises(PublishBlocked) as raised:
            change_definition(draft.uuid, expected_version=draft.definition_version, definition=published_definition)
        self.assertIn("自測", raised.exception.user_message)

    @cloud_only
    def test_builder_warns_node_draft_outside_scope(self):
        manager = User.objects.create_user(username="m", password="x", role="manager")
        self.client.force_login(manager)
        definition = blank_definition(uuid.uuid4(), title="節點草稿")
        draft = create_node_survey(self.node, definition)[0].survey
        page = self.client.get(reverse("feedback:survey-builder", args=[draft.slug]))
        self.assertContains(page, "只開放自測問卷")
