import tempfile
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from cloudsync.capture import capture_scope
from cloudsync.definitions import upsert_definition
from cloudsync.inbox import intake
from cloudsync.models import SurveySyncState
from cloudsync.runner import _apply_abandoned
from cloudsync.scope import is_cloud_synced
from cloudsync.tests.test_inbox import Q1, U1, U2, U3, FakeInboxClient, definition, envelope
from feedback.analysis_adapters import AnswerInput
from feedback.models import AnalysisJob, FeedbackSubmission, KeywordCategory, Question, Survey, SurveyAIReportSnapshot


class ScopeTests(TestCase):
    def test_only_synced_answer_surveys_are_in_scope(self):
        synced, _ = upsert_definition(definition(1))
        local = Survey.objects.create(title="L", slug="local")
        self.assertEqual((is_cloud_synced(synced), is_cloud_synced(local)), (True, False))


class CaptureTests(TestCase):
    def setUp(self):
        self.survey, _ = upsert_definition(definition(1))
        intake(envelope(1, U1, {Q1: "a"}), FakeInboxClient([]))
        intake(envelope(3, U3, {Q1: "c"}), FakeInboxClient([]))  # gap at 2 keeps W at 1
        FeedbackSubmission.objects.create(survey=self.survey, is_complete=False)
        voided = FeedbackSubmission.objects.create(survey=self.survey, is_complete=False)
        FeedbackSubmission.objects.filter(pk=voided.pk).update(voided_at="2026-10-01T00:00:00+00:00")

    def test_scope_freezes_the_watermark_and_counts_exclusions_once(self):
        scope = capture_scope(self.survey)
        self.assertEqual((scope.watermark, scope.definition_version), (1, 1))
        self.assertEqual(scope.excluded, {"voided": 1, "incomplete": 1})

    def test_adapter_reads_only_up_to_the_frozen_watermark(self):
        scope = capture_scope(self.survey)
        intake(envelope(2, U2, {Q1: "b"}), FakeInboxClient([]))
        adapter = AnswerInput.from_survey(Survey.objects.get(pk=self.survey.pk), version="v",
                                          extra_filter=scope.submission_filter)
        name = next(iter(adapter.question_fields.values())).name
        self.assertEqual([row[name] for row in adapter.scan([name])], ["a"])

    def test_watermark_advance_schedules_analysis(self):
        AnalysisJob.objects.all().delete()
        state = SurveySyncState.objects.get(survey=self.survey)
        _apply_abandoned({"surveys": [{"survey_uuid": str(self.survey.uuid), "abandoned_sequences": [2]}]})
        state.refresh_from_db()
        self.assertEqual(state.synced_through_sequence, 3)
        self.assertTrue(AnalysisJob.objects.filter(survey=self.survey, status="pending").exists())


class InterleavingTests(TestCase):
    """Mutations between the statistics and text reads of one worker run (spec §13 新鮮度)."""

    def setUp(self):
        self.survey, _ = upsert_definition(definition(1))
        intake(envelope(1, U1, {Q1: "a"}), FakeInboxClient([]))
        self.output = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.output.cleanup)

    def run_worker_with(self, mutate):
        from feedback import analysis_worker

        original = analysis_worker.calculate_text

        def text_with_mutation(*args, **kwargs):
            mutate()
            return original(*args, **kwargs)

        with patch.object(analysis_worker, "calculate_text", text_with_mutation):
            call_command("run_analysis_worker_once", worker_id="t", output=self.output.name)

    def published_scope(self):
        snapshot = SurveyAIReportSnapshot.objects.filter(survey=self.survey).order_by("-pk").first()
        state = self.survey.analysis_state
        state.refresh_from_db()
        return state.published_snapshot_id == getattr(snapshot, "pk", None), snapshot

    def test_reply_that_does_not_advance_the_watermark_is_excluded(self):
        self.run_worker_with(lambda: intake(envelope(3, U3, {Q1: "c"}), FakeInboxClient([])))  # gap at 2: W stays 1
        published, snapshot = self.published_scope()
        self.assertTrue(published)
        self.assertEqual((snapshot.response_count, snapshot.source_snapshot["data_scope"]["analyzed_through_sequence"]), (1, 1))

    def test_reply_that_advances_the_watermark_voids_this_run_and_the_next_includes_it(self):
        self.run_worker_with(lambda: intake(envelope(2, U2, {Q1: "b"}), FakeInboxClient([])))  # W 1 -> 2
        self.assertFalse(self.published_scope()[0])
        self.assertTrue(AnalysisJob.objects.filter(survey=self.survey, status="pending").exists())
        call_command("run_analysis_worker_once", worker_id="t", output=self.output.name)
        published, snapshot = self.published_scope()
        self.assertTrue(published)
        self.assertEqual((snapshot.response_count, snapshot.source_snapshot["data_scope"]["analyzed_through_sequence"]), (2, 2))

    def test_dictionary_change_blocks_publication(self):
        self.run_worker_with(lambda: KeywordCategory.objects.create(survey=self.survey, keyword="甜", category="口味"))
        self.assertFalse(self.published_scope()[0])

    def test_question_change_blocks_publication(self):
        def rename():
            question = Question.objects.get(uuid=Q1)
            question.title = "新題名"
            question.save()  # post_save signal bumps the version in the same transaction

        self.run_worker_with(rename)
        self.assertFalse(self.published_scope()[0])
