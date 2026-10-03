"""Inbox with published surveys and answers_format 2 (builder spec §4, §7.1, §7.2, §7.3)."""

import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from cloudapi.definition import add_question, serialize_definition
from cloudapi.envelope import ANSWERS_FORMAT, payload_hash
from cloudapi.freshness import node_freshness
from cloudapi.inbox import SurveyClosed, accept_submission
from cloudapi.models import InboxCounter, InboxSubmission, NodeDevice, SubmissionReceipt
from cloudapi.writes import assign_survey_to_node, change_definition
from feedback.models import Survey, SurveyAnalysisState

User = get_user_model()


def edit(survey, **fields):
    definition = serialize_definition(survey)
    definition.update(fields)
    change_definition(survey.uuid, expected_version=survey.definition_version, definition=definition)
    return Survey.objects.get(pk=survey.pk)


@override_settings(CLOUD_INBOX_ENABLED=True)
class PublishedInboxTests(TestCase):
    def setUp(self):
        self.node, _ = NodeDevice.issue("office")
        survey = assign_survey_to_node(Survey.objects.create(title="S", slug="s"), self.node).survey
        definition = serialize_definition(survey)
        add_question(definition, {"title": "門市", "kind": "single_choice",
                                  "choices": [{"code": "", "label": "信義"}, {"code": "", "label": "公館"}]})
        change_definition(survey.uuid, expected_version=survey.definition_version, definition=definition)
        self.survey = edit(Survey.objects.get(pk=survey.pk), published=True)
        self.question = self.survey.questions.get()
        self.user = User.objects.create_user(username="c", password="x")

    def accept(self, **overrides):
        kwargs = {"user": self.user, "submission_uuid": uuid.uuid4(), "form_version": self.survey.published_version,
                  "consent_follow_up": False, "answers": {str(self.question.uuid): "c2"}, **overrides}
        return accept_submission(Survey.objects.get(pk=self.survey.pk), **kwargs)

    def test_envelope_carries_answers_format_and_codes(self):
        self.accept()
        envelope = InboxSubmission.objects.get().envelope
        self.assertEqual((envelope["answers_format"], envelope["answers"]), (ANSWERS_FORMAT, {str(self.question.uuid): "c2"}))
        self.assertEqual(envelope["definition_version"], self.survey.published_version)

    def test_payload_hash_covers_answers_format(self):
        base = dict(survey_uuid=self.survey.uuid, definition_version=1, consent_follow_up=False, is_complete=True,
                    voided_at=None, answers={"x": "c1"})
        self.assertNotEqual(payload_hash(**base, answers_format=2), payload_hash(**base, answers_format=1))

    def test_whitelist_change_does_not_reject_open_form(self):
        form_version = self.survey.published_version
        survey = edit(self.survey, is_active=False)
        self.survey = edit(survey, is_active=True)
        self.assertGreater(self.survey.definition_version, form_version)
        self.assertFalse(self.accept(form_version=form_version).reused)

    def test_closed_survey_rejected_under_lock(self):
        self.survey = edit(self.survey, is_active=False)
        with self.assertRaises(SurveyClosed):
            self.accept()
        self.assertFalse(SubmissionReceipt.objects.exists())

    def test_resend_same_payload_reuses_receipt_after_whitelist_change(self):
        key = uuid.uuid4()
        first = self.accept(submission_uuid=key)
        self.survey = edit(self.survey, thank_you_email_enabled=False)
        again = self.accept(submission_uuid=key)
        self.assertEqual((again.reused, again.receipt.pk), (True, first.receipt.pk))

    def test_resend_after_close_or_archive_reuses_receipt_without_new_capacity(self):
        key = uuid.uuid4()
        self.accept(submission_uuid=key)
        counter = InboxCounter.objects.get(node=self.node)
        occupied = (counter.occupied_count, counter.occupied_bytes)
        self.survey = edit(self.survey, is_active=False)
        self.assertTrue(self.accept(submission_uuid=key).reused)
        self.survey = edit(self.survey, archived_at="2026-10-03T00:00:00+00:00")
        self.assertTrue(self.accept(submission_uuid=key).reused)
        counter.refresh_from_db()
        self.assertEqual((counter.occupied_count, counter.occupied_bytes), occupied)
        self.assertEqual(SubmissionReceipt.objects.count(), 1)


@override_settings(CLOUD_INBOX_ENABLED=True)
class NodeFreshnessTests(PublishedInboxTests):
    def result_state(self, definition_version):
        state, _ = SurveyAnalysisState.objects.get_or_create(survey=self.survey)
        state.published_upload_uuid = uuid.uuid4()
        state.definition_version = definition_version
        state.analyzed_through_sequence = 0
        state.save()
        return state

    def test_freshness_ignores_status_category_email_tracking_changes(self):
        state = self.result_state(self.survey.analysis_definition_version)
        self.survey = edit(self.survey, is_active=False, category="門市", thank_you_email_enabled=False,
                           improvement_tracking_enabled=False)
        self.assertTrue(node_freshness(self.survey, state)["definition_current"])
        self.assertNotIn("legacy_unmigrated", node_freshness(self.survey, state))

    def test_analysis_disabled_or_archived_marks_node_result_stale(self):
        uploaded_against = self.survey.analysis_definition_version
        state = self.result_state(uploaded_against)
        self.survey = edit(self.survey, analysis_enabled=False)
        self.assertFalse(node_freshness(self.survey, state)["definition_current"])
        # A result computed against the old settings and uploaded late is still not current.
        late = self.result_state(uploaded_against)
        self.assertFalse(node_freshness(self.survey, late)["definition_current"])
        self.assertTrue(node_freshness(self.survey, self.result_state(self.survey.analysis_definition_version))
                        ["definition_current"])
