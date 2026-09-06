from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from .ai_snapshot_service import SNAPSHOT_SCHEMA_VERSION, current_prompt_version
from .ai_worker import AIWorkerExecutionError, execute_ai_job
from .analysis_jobs import claim_next_job, publish_analysis_snapshot, schedule_survey_analysis
from .models import AnalysisJob, Survey, SurveyAIAnalysisStage, SurveyAIReportSnapshot, SurveyAnalysisState
from .test_ai_stages import provider_response, statistics_payload, synthesis_payload, text_payload
from .tests import source_snapshot


@override_settings(AI_REPORT_REQUEST_INTERVAL_SECONDS=0)
class AIWorkerTests(TestCase):
    def setUp(self):
        self.survey = Survey.objects.create(title="AI worker fixture", slug="ai-worker-fixture")

    def prepare_ai_claim(self):
        schedule_survey_analysis(self.survey.pk, change="input")
        base_job = claim_next_job(
            "deterministic-worker",
            lease_seconds=60,
            executor=AnalysisJob.Executor.DETERMINISTIC,
        )
        payload = source_snapshot(self.survey.slug)
        payload.update({"statistics": {}, "text_analysis": {}})
        snapshot = SurveyAIReportSnapshot.objects.create(
            survey=self.survey,
            data_fingerprint="a" * 64,
            snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
            prompt_version=current_prompt_version(),
            model_name=settings.GEMINI_MODEL,
            source_snapshot=payload,
            status=SurveyAIReportSnapshot.Status.SNAPSHOT_READY,
            response_count=6,
        )
        publish_analysis_snapshot(
            base_job.pk,
            base_job.lease_token,
            snapshot_id=snapshot.pk,
            input_fingerprint=snapshot.data_fingerprint,
        )
        schedule_survey_analysis(
            self.survey.pk,
            change="none",
            requested_stages=(SurveyAIAnalysisStage.StageType.SYNTHESIS,),
        )
        ai_job = claim_next_job(
            "ai-worker",
            lease_seconds=60,
            executor=AnalysisJob.Executor.AI,
        )
        return snapshot, ai_job

    @patch("feedback.ai_stage_service.create_gemini_client")
    def test_authorized_worker_generates_existing_stages_and_publishes_synthesis(self, client_factory):
        snapshot, job = self.prepare_ai_claim()
        client_factory.return_value.models.generate_content.side_effect = [
            provider_response(statistics_payload()),
            provider_response(text_payload()),
            provider_response(synthesis_payload()),
        ]

        result = execute_ai_job(job, allow_paid_ai=True, lease_seconds=60)

        self.assertEqual(client_factory.return_value.models.generate_content.call_count, 3)
        self.assertEqual(set(result.stage_ids), {"statistics", "text", "synthesis"})
        state = SurveyAnalysisState.objects.get(survey=self.survey)
        self.assertEqual(state.published_snapshot, snapshot)
        self.assertEqual(state.published_ai_stage_id, result.stage_ids["synthesis"])
        self.assertEqual(state.published_ai_payload["executive_summary"], synthesis_payload()["executive_summary"])
        self.assertNotIn("_evidence_registry", repr(state.published_ai_payload))
        self.assertEqual(state.publication_manifest["ai"]["model_name"], settings.GEMINI_MODEL)
        job.refresh_from_db()
        self.assertEqual(job.status, AnalysisJob.Status.SUCCEEDED)

    @patch("feedback.ai_stage_service.create_gemini_client")
    def test_missing_paid_authorization_never_calls_provider(self, client_factory):
        _, job = self.prepare_ai_claim()
        with self.assertRaises(AIWorkerExecutionError) as raised:
            execute_ai_job(job, allow_paid_ai=False, lease_seconds=60)
        self.assertEqual(raised.exception.code, "paid_ai_not_authorized")
        client_factory.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, AnalysisJob.Status.RUNNING)

    def test_command_without_paid_flag_does_not_claim_pending_ai_job(self):
        _, job = self.prepare_ai_claim()
        AnalysisJob.objects.filter(pk=job.pk).update(
            status=AnalysisJob.Status.PENDING,
            worker_id="",
            lease_token=None,
            lease_expires_at=None,
            heartbeat_at=None,
            attempt_count=0,
        )
        with self.assertRaises(CommandError):
            call_command("run_ai_worker_once", worker_id="ai-worker", lease_seconds=60)
        job.refresh_from_db()
        self.assertEqual(job.status, AnalysisJob.Status.PENDING)
        self.assertEqual(job.attempt_count, 0)

    @patch("feedback.ai_stage_service.create_gemini_client")
    def test_stale_base_is_rejected_before_provider_call(self, client_factory):
        _, _ = self.prepare_ai_claim()
        schedule_survey_analysis(self.survey.pk, change="input")
        schedule_survey_analysis(
            self.survey.pk,
            change="none",
            requested_stages=(SurveyAIAnalysisStage.StageType.SYNTHESIS,),
        )
        ai_job = claim_next_job(
            "new-ai-worker",
            lease_seconds=60,
            executor=AnalysisJob.Executor.AI,
        )
        with self.assertRaises(AIWorkerExecutionError) as raised:
            execute_ai_job(ai_job, allow_paid_ai=True, lease_seconds=60)
        self.assertEqual(raised.exception.code, "base_snapshot_not_current")
        client_factory.assert_not_called()
