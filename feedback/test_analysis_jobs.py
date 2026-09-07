from datetime import timedelta
import json
from pathlib import Path
import tempfile

from django.core.management import call_command
from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from .analysis_jobs import (
    AnalysisJobError,
    claim_next_job,
    fail_job,
    finish_cancelled_job,
    heartbeat_job,
    publish_analysis_snapshot,
    publish_analysis_stages,
    request_job_cancel,
    schedule_survey_analysis,
    suppress_analysis_scheduling,
)
from .local_service import submit_survey_payload
from .models import (
    AnalysisJob,
    Answer,
    FeedbackSubmission,
    KeywordCategory,
    Question,
    Survey,
    SurveyAIAnalysisStage,
    SurveyAIReportSnapshot,
    SurveyAnalysisState,
)


class AnalysisJobCoordinationTests(TestCase):
    def setUp(self):
        self.survey = Survey.objects.create(title="Queue fixture", slug="queue-fixture")

    def make_snapshot(self, marker, stage_types):
        snapshot = SurveyAIReportSnapshot.objects.create(
            survey=self.survey,
            data_fingerprint=marker * 64,
            snapshot_schema_version="fixture-v1",
            prompt_version="fixture-v1",
            model_name="fixture-model",
            source_snapshot={
                "data_scope": {"response_count": 1},
                "statistics": {"charts": []},
                "text_analysis": {"keywords": []},
            },
            status=SurveyAIReportSnapshot.Status.SNAPSHOT_READY,
            response_count=1,
        )
        stages = {}
        for stage_type in stage_types:
            stages[stage_type] = SurveyAIAnalysisStage.objects.create(
                snapshot=snapshot,
                stage_type=stage_type,
                status=SurveyAIAnalysisStage.Status.SUCCEEDED,
                input_hash=(marker + stage_type[0]) * 32,
                schema_version="fixture-v1",
                prompt_version="fixture-v1",
                model_name="fixture-model",
                output_json={"summary": stage_type},
                generated_at=timezone.now(),
            )
        return snapshot, stages

    def test_mutations_coalesce_one_pending_job_and_bump_stored_versions(self):
        first = schedule_survey_analysis(self.survey.pk, change="input")
        second = schedule_survey_analysis(self.survey.pk, change="config")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(AnalysisJob.objects.count(), 1)
        state = SurveyAnalysisState.objects.get(survey=self.survey)
        self.assertEqual((state.input_version, state.config_version), (1, 1))
        second.refresh_from_db()
        self.assertEqual((second.input_version, second.config_version), (1, 1))

    def test_ai_and_deterministic_work_have_independent_pending_rows(self):
        schedule_survey_analysis(self.survey.pk, change="input")
        schedule_survey_analysis(
            self.survey.pk,
            change="none",
            requested_stages=(SurveyAIAnalysisStage.StageType.SYNTHESIS,),
        )
        self.assertEqual(AnalysisJob.objects.filter(status=AnalysisJob.Status.PENDING).count(), 2)
        self.assertEqual(
            set(AnalysisJob.objects.values_list("executor", flat=True)),
            {AnalysisJob.Executor.DETERMINISTIC, AnalysisJob.Executor.AI},
        )

    def test_question_and_keyword_changes_invalidate_configuration(self):
        Question.objects.create(
            survey=self.survey,
            title="Comment",
            kind=Question.Kind.LONG_TEXT,
            data_type=Question.DataType.TEXT,
        )
        KeywordCategory.objects.create(survey=self.survey, keyword="clean", category="room")
        state = SurveyAnalysisState.objects.get(survey=self.survey)
        self.assertEqual(state.config_version, 2)
        self.assertEqual(AnalysisJob.objects.filter(status=AnalysisJob.Status.PENDING).count(), 1)

    def test_keyword_sync_commits_one_configuration_version(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "keywords.json"
            source.write_text(
                json.dumps(
                    {
                        "survey": self.survey.slug,
                        "threshold": 2,
                        "mappings": {"clean": "room", "friendly": "service"},
                    }
                ),
                encoding="utf-8",
            )
            call_command("sync_keyword_categories", file=str(source))
        state = SurveyAnalysisState.objects.get(survey=self.survey)
        self.assertEqual(state.config_version, 1)
        self.assertEqual(KeywordCategory.objects.filter(survey=self.survey).count(), 2)
        self.assertEqual(AnalysisJob.objects.filter(status=AnalysisJob.Status.PENDING).count(), 1)

    def test_submission_service_schedules_once_after_complete_transaction(self):
        with suppress_analysis_scheduling():
            first = Question.objects.create(
                survey=self.survey,
                title="Score",
                kind=Question.Kind.SCALE,
                data_type=Question.DataType.ORDINAL,
                options_text="1\n2\n3\n4\n5",
            )
            second = Question.objects.create(
                survey=self.survey,
                title="Comment",
                kind=Question.Kind.LONG_TEXT,
                data_type=Question.DataType.TEXT,
            )
        submit_survey_payload(
            self.survey,
            user=None,
            respondent_name="",
            respondent_email="",
            consent_follow_up=False,
            answers={f"question_{first.pk}": "4", f"question_{second.pk}": "clean room"},
        )
        state = SurveyAnalysisState.objects.get(survey=self.survey)
        self.assertEqual(state.input_version, 1)
        self.assertEqual(AnalysisJob.objects.filter(status=AnalysisJob.Status.PENDING).count(), 1)

    def test_derived_answer_cache_update_does_not_invalidate_source_version(self):
        with suppress_analysis_scheduling():
            question = Question.objects.create(
                survey=self.survey,
                title="Comment",
                kind=Question.Kind.LONG_TEXT,
                data_type=Question.DataType.TEXT,
            )
            submission = FeedbackSubmission.objects.create(survey=self.survey)
            answer = Answer.objects.create(submission=submission, question=question, value="clean room")
        answer.analysis_text = "clean room"
        answer.analysis_version = "fixture"
        answer.save(update_fields=("analysis_text", "analysis_version"))
        self.assertFalse(SurveyAnalysisState.objects.filter(survey=self.survey).exists())

        answer.value = "updated clean room"
        answer.save(update_fields=("value",))
        state = SurveyAnalysisState.objects.get(survey=self.survey)
        self.assertEqual(state.input_version, 1)

    def test_schedule_rolls_back_with_source_transaction(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                schedule_survey_analysis(self.survey.pk, change="input")
                raise RuntimeError("rollback")
        self.assertFalse(SurveyAnalysisState.objects.filter(survey=self.survey).exists())
        self.assertFalse(AnalysisJob.objects.exists())

    def test_claim_heartbeat_and_cancel_require_current_lease(self):
        schedule_survey_analysis(self.survey.pk)
        claimed = claim_next_job("worker-fixture", lease_seconds=30)
        self.assertEqual(claimed.status, AnalysisJob.Status.RUNNING)
        self.assertTrue(heartbeat_job(claimed.pk, claimed.lease_token, lease_seconds=30).accepted)
        request_job_cancel(claimed.pk)
        heartbeat = heartbeat_job(claimed.pk, claimed.lease_token, lease_seconds=30)
        self.assertFalse(heartbeat.accepted)
        self.assertTrue(heartbeat.cancel_requested)
        self.assertTrue(finish_cancelled_job(claimed.pk, claimed.lease_token))
        claimed.refresh_from_db()
        self.assertEqual(claimed.status, AnalysisJob.Status.CANCELLED)

    def test_claim_can_select_exact_external_source_without_taking_answer_job(self):
        answer_job = schedule_survey_analysis(self.survey.pk, change="input")
        external_job = schedule_survey_analysis(
            self.survey.pk,
            change="none",
            source_kind=AnalysisJob.SourceKind.EXTERNAL,
            source_ref="fixture/reviews",
            source_version="source-v1",
        )

        claimed = claim_next_job(
            "external-worker",
            lease_seconds=30,
            executor=AnalysisJob.Executor.DETERMINISTIC,
            survey_ids=(self.survey.pk,),
            source_kind=AnalysisJob.SourceKind.EXTERNAL,
            source_ref="fixture/reviews",
        )

        self.assertEqual(claimed.pk, external_job.pk)
        answer_job.refresh_from_db()
        self.assertEqual(answer_job.status, AnalysisJob.Status.PENDING)

    def test_retry_is_finite_and_does_not_store_raw_error_text(self):
        job = schedule_survey_analysis(self.survey.pk)
        job.max_attempts = 2
        job.save(update_fields=("max_attempts",))
        first = claim_next_job("worker-fixture", lease_seconds=30)
        self.assertTrue(
            fail_job(
                first.pk,
                first.lease_token,
                error_code="Timeout: secret=do-not-log",
                retryable=True,
            )
        )
        second = claim_next_job("worker-fixture", lease_seconds=30)
        self.assertTrue(fail_job(second.pk, second.lease_token, error_code="provider failed", retryable=True))
        second.refresh_from_db()
        self.assertEqual(second.status, AnalysisJob.Status.FAILED)
        self.assertEqual(second.attempt_count, 2)
        self.assertNotIn("=", second.error_code)

    def test_expired_lease_is_reclaimed_and_old_token_is_rejected(self):
        schedule_survey_analysis(self.survey.pk)
        first = claim_next_job("first-worker", lease_seconds=30)
        old_token = first.lease_token
        AnalysisJob.objects.filter(pk=first.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )
        second = claim_next_job("second-worker", lease_seconds=30)
        self.assertEqual(second.pk, first.pk)
        self.assertNotEqual(second.lease_token, old_token)
        self.assertFalse(heartbeat_job(first.pk, old_token, lease_seconds=30).accepted)
        self.assertFalse(fail_job(first.pk, old_token, error_code="stale-worker"))

    def test_stale_worker_cannot_publish_and_new_version_remains_queued(self):
        job = schedule_survey_analysis(
            self.survey.pk,
            requested_stages=(SurveyAIAnalysisStage.StageType.STATISTICS,),
        )
        claimed = claim_next_job("old-worker", lease_seconds=30)
        snapshot, _ = self.make_snapshot("a", ())
        schedule_survey_analysis(self.survey.pk, change="input")
        result = publish_analysis_snapshot(
            claimed.pk,
            claimed.lease_token,
            snapshot_id=snapshot.pk,
        )
        self.assertTrue(result.stale)
        claimed.refresh_from_db()
        self.assertEqual((claimed.status, claimed.error_code), (AnalysisJob.Status.CANCELLED, "superseded"))
        self.assertTrue(AnalysisJob.objects.filter(status=AnalysisJob.Status.PENDING, input_version=2).exists())
        self.assertIsNone(SurveyAnalysisState.objects.get(survey=self.survey).published_snapshot)

    def test_snapshot_publication_is_atomic_and_preserves_previous_ai_pointer(self):
        old_snapshot, old_stages = self.make_snapshot("b", (SurveyAIAnalysisStage.StageType.SYNTHESIS,))
        schedule_survey_analysis(self.survey.pk)
        state = SurveyAnalysisState.objects.get(survey=self.survey)
        state.published_ai_stage = old_stages[SurveyAIAnalysisStage.StageType.SYNTHESIS]
        state.publication_manifest = {"ai": {"input_version": 0}}
        state.save(update_fields=("published_ai_stage", "publication_manifest"))
        claimed = claim_next_job("publisher", lease_seconds=30)
        snapshot, _ = self.make_snapshot("c", ())
        with self.assertRaises(AnalysisJobError):
            publish_analysis_snapshot(
                claimed.pk,
                claimed.lease_token,
                snapshot_id=snapshot.pk,
                input_fingerprint="f" * 64,
            )
        claimed.refresh_from_db()
        self.assertEqual(claimed.status, AnalysisJob.Status.RUNNING)
        result = publish_analysis_snapshot(
            claimed.pk,
            claimed.lease_token,
            snapshot_id=snapshot.pk,
            input_fingerprint=snapshot.data_fingerprint,
        )
        self.assertTrue(result.published)
        state.refresh_from_db()
        self.assertEqual(state.published_snapshot, snapshot)
        self.assertEqual(state.published_ai_stage, old_stages[SurveyAIAnalysisStage.StageType.SYNTHESIS])
        self.assertEqual(state.publication_manifest["ai"]["input_version"], 0)
        self.assertEqual(state.publication_manifest["statistics"]["snapshot_id"], snapshot.pk)

    def test_mock_stage_is_rejected_for_formal_publication(self):
        schedule_survey_analysis(self.survey.pk)
        base_claim = claim_next_job("publisher", lease_seconds=30)
        snapshot, _ = self.make_snapshot("d", ())
        publish_analysis_snapshot(
            base_claim.pk,
            base_claim.lease_token,
            snapshot_id=snapshot.pk,
        )
        schedule_survey_analysis(
            self.survey.pk,
            change="none",
            requested_stages=(SurveyAIAnalysisStage.StageType.SYNTHESIS,),
        )
        claimed = claim_next_job("publisher", lease_seconds=30)
        stage = SurveyAIAnalysisStage.objects.create(
            snapshot=snapshot,
            stage_type=SurveyAIAnalysisStage.StageType.SYNTHESIS,
            status=SurveyAIAnalysisStage.Status.SUCCEEDED,
            input_hash="e" * 64,
            schema_version="fixture-v1",
            prompt_version="fixture-v1",
            model_name="mock",
            output_json={"ai_mode": "mock"},
        )
        with self.assertRaises(AnalysisJobError):
            publish_analysis_stages(
                claimed.pk,
                claimed.lease_token,
                snapshot_id=snapshot.pk,
                stage_ids={SurveyAIAnalysisStage.StageType.SYNTHESIS: stage.pk},
            )

    def test_ai_publication_projects_only_safe_display_fields(self):
        schedule_survey_analysis(self.survey.pk)
        base_claim = claim_next_job("publisher", lease_seconds=30)
        snapshot, _ = self.make_snapshot("z", ())
        publish_analysis_snapshot(base_claim.pk, base_claim.lease_token, snapshot_id=snapshot.pk)
        schedule_survey_analysis(
            self.survey.pk,
            change="none",
            requested_stages=(SurveyAIAnalysisStage.StageType.SYNTHESIS,),
        )
        claimed = claim_next_job("publisher", lease_seconds=30)
        stage = SurveyAIAnalysisStage.objects.create(
            snapshot=snapshot,
            stage_type=SurveyAIAnalysisStage.StageType.SYNTHESIS,
            status=SurveyAIAnalysisStage.Status.SUCCEEDED,
            input_hash="p" * 64,
            schema_version="fixture-v1",
            prompt_version="fixture-v1",
            model_name="fixture-model",
            output_json={
                "executive_summary": "安全摘要",
                "combined_findings": [
                    {
                        "title": "發現",
                        "priority": "high",
                        "private_marker": "must-not-publish",
                        "evidence": [{"id": "safe.ref", "value": 4, "raw_comment": "secret"}],
                    }
                ],
                "improvement_drafts": [],
                "data_caveats": [],
                "_evidence_registry": {"secret": "must-not-publish"},
            },
            generated_at=timezone.now(),
        )
        publish_analysis_stages(
            claimed.pk,
            claimed.lease_token,
            snapshot_id=snapshot.pk,
            stage_ids={SurveyAIAnalysisStage.StageType.SYNTHESIS: stage.pk},
        )
        payload = SurveyAnalysisState.objects.get(survey=self.survey).published_ai_payload
        self.assertEqual(payload["combined_findings"][0]["evidence"][0], {"id": "safe.ref", "value": 4})
        self.assertNotIn("must-not-publish", repr(payload))
        self.assertNotIn("raw_comment", repr(payload))
