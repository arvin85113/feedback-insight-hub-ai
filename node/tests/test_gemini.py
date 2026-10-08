from unittest.mock import MagicMock, patch
import uuid

from django.test import override_settings
from django.urls import reverse

from cloudsync.tests.utils import memory_keyring
from feedback.ai_worker import AIWorkerExecutionError
from feedback.analysis_jobs import claim_next_job
from feedback.models import AnalysisJob, Survey, SurveyAnalysisState
from feedback import test_ai_worker as worker_tests
from feedback.test_ai_stages import provider_response, statistics_payload, text_payload, synthesis_payload
from node.gemini import AuthorizedClient, confirm, current_identity, execute_confirmed, set_key
from node.models import NodeAIGrant, NodeAuditEvent
from node.tests.test_console import ConsoleTestCase, Role


@override_settings(AI_REPORT_REQUEST_INTERVAL_SECONDS=0)
class GeminiTests(ConsoleTestCase):
    def setUp(self):
        super().setUp()
        vault = memory_keyring()
        vault.__enter__()
        self.addCleanup(vault.__exit__, None, None, None)
        self.client.force_login(self.owner)
        self.survey = Survey.objects.create(title="AI fixture", slug="ai-fixture")
        self.snapshot, ai_job = worker_tests.AIWorkerTests.prepare_ai_claim(self)
        AnalysisJob.objects.filter(pk=ai_job.pk).update(status="pending", lease_token=None, lease_expires_at=None)
        set_key("fixture-key-not-real")

    def grant(self):
        return confirm(self.survey.pk, self.owner, current_identity(self.survey.pk))

    def claim(self):
        grant = self.grant()
        job = claim_next_job("confirmed-test", executor="ai", job_ids=[grant.job_id], lease_seconds=60)
        return grant, job

    def test_settings_owner_only_key_never_echoed_or_audited(self):
        response = self.client.post(reverse("node:gemini-settings"), {"action": "save", "api_key": "private-test-secret"})
        self.assertEqual(response.status_code, 302)
        response = self.client.get(reverse("node:gemini-settings"))
        self.assertContains(response, "已設定")
        self.assertNotContains(response, "private-test-secret")
        self.assertNotIn("private-test-secret", repr(list(NodeAuditEvent.objects.values())))
        admin = self._member("admin-ai@fixture.invalid", Role.ADMIN)
        self.client.force_login(admin)
        self.assertEqual(self.client.get(reverse("node:gemini-settings")).status_code, 403)

    def test_signed_confirmation_is_idempotent_and_preview_does_not_queue_grant(self):
        response = self.client.post(reverse("node:jobs"), {"action": "preview-ai", "survey": self.survey.pk})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(NodeAIGrant.objects.exists())
        data = {"action": "confirm-ai", "survey": self.survey.pk, "confirmation": response.context["confirmation"]}
        self.assertEqual(self.client.post(reverse("node:jobs"), data).status_code, 302)
        self.assertEqual(self.client.post(reverse("node:jobs"), data).status_code, 302)
        self.assertEqual(NodeAIGrant.objects.count(), 1)

    def test_version_or_credential_change_invalidates_confirmation(self):
        identity = current_identity(self.survey.pk)
        set_key("replacement-fixture")
        with self.assertRaises(AIWorkerExecutionError):
            confirm(self.survey.pk, self.owner, identity)
        identity = current_identity(self.survey.pk)
        SurveyAnalysisState.objects.filter(survey=self.survey).update(input_version=99)
        with self.assertRaises(AIWorkerExecutionError):
            confirm(self.survey.pk, self.owner, identity)
        self.assertFalse(NodeAIGrant.objects.exists())

    @patch("google.genai.Client")
    def test_confirmed_pipeline_three_calls_publishes_and_keeps_safe_audit(self, factory):
        grant, job = self.claim()
        factory.return_value.models.generate_content.side_effect = [
            provider_response(statistics_payload()), provider_response(text_payload()), provider_response(synthesis_payload())]
        result = execute_confirmed(job, lease_seconds=60)
        grant.refresh_from_db()
        self.assertEqual((grant.status, grant.calls_started, grant.in_flight), ("completed", 3, False))
        self.assertEqual(SurveyAnalysisState.objects.get(survey=self.survey).published_ai_stage_id, result.stage_ids["synthesis"])
        self.assertNotIn("fixture-key", repr(list(NodeAuditEvent.objects.values())))

    def test_timeout_preserves_uncertainty_and_never_repeats_provider(self):
        grant, job = self.claim()
        provider = MagicMock()
        provider.models.generate_content.side_effect = TimeoutError()
        client = AuthorizedClient(provider, job, 60)
        with self.assertRaises(TimeoutError):
            client.generate_content(model=grant.identity["model"])
        with self.assertRaises(AIWorkerExecutionError):
            client.generate_content(model=grant.identity["model"])
        self.assertEqual(provider.models.generate_content.call_count, 1)
        grant.refresh_from_db()
        self.assertEqual((grant.status, grant.in_flight), ("uncertain", True))

    def test_budget_and_changed_data_prevent_provider_call(self):
        grant, job = self.claim()
        provider = MagicMock()
        NodeAIGrant.objects.filter(pk=grant.pk).update(calls_started=grant.max_calls)
        with self.assertRaises(AIWorkerExecutionError):
            AuthorizedClient(provider, job, 60).generate_content(model=grant.identity["model"])
        provider.models.generate_content.assert_not_called()

    def test_data_change_between_calls_stops_next_provider_call(self):
        grant, job = self.claim()
        provider = MagicMock()
        client = AuthorizedClient(provider, job, 60)
        client.generate_content(model=grant.identity["model"])
        SurveyAnalysisState.objects.filter(survey=self.survey).update(input_version=99)
        with self.assertRaises(AIWorkerExecutionError):
            client.generate_content(model=grant.identity["model"])
        self.assertEqual(provider.models.generate_content.call_count, 1)

    def test_owner_revocation_prevents_provider_call(self):
        grant, job = self.claim()
        self.owner.is_active = False
        self.owner.save(update_fields=["is_active"])
        provider = MagicMock()
        with self.assertRaises(AIWorkerExecutionError):
            AuthorizedClient(provider, job, 60).generate_content(model=grant.identity["model"])
        provider.models.generate_content.assert_not_called()

    def test_uncertain_call_blocks_new_confirmation(self):
        grant, _ = self.claim()
        NodeAIGrant.objects.filter(pk=grant.pk).update(status="uncertain", in_flight=True)
        with self.assertRaises(AIWorkerExecutionError):
            confirm(self.survey.pk, self.owner, current_identity(self.survey.pk), confirmation_uuid=uuid.uuid4())
        self.assertEqual(NodeAIGrant.objects.count(), 1)

    @override_settings(GOOGLE_API_KEY="")
    @patch("google.genai.Client")
    def test_worker_executes_confirmed_grant_without_cli_paid_flag(self, factory):
        from django.core.management import call_command
        grant = self.grant()
        factory.return_value.models.generate_content.side_effect = [
            provider_response(statistics_payload()), provider_response(text_payload()), provider_response(synthesis_payload())]
        call_command("run_analysis_worker", worker_id="fixture", output=str(self.paths.artifacts_dir), once=True)
        grant.refresh_from_db()
        grant.job.refresh_from_db()
        self.assertEqual((grant.status, grant.job.status, grant.calls_started), ("completed", AnalysisJob.Status.SUCCEEDED, 3))

    def test_changed_input_finalizes_invalid_grant_without_provider_call(self):
        grant, job = self.claim()
        SurveyAnalysisState.objects.filter(survey=self.survey).update(input_version=99)
        with patch("google.genai.Client") as factory, self.assertRaises(AIWorkerExecutionError):
            execute_confirmed(job)
        factory.assert_not_called()
        grant.refresh_from_db()
        self.assertEqual((grant.status, grant.in_flight), ("failed", False))

    def test_invalid_job_and_survey_ids_are_not_server_errors(self):
        for data in ({"action": "cancel", "job": "invalid"}, {"action": "analyse", "survey": "invalid"}):
            self.assertEqual(self.client.post(reverse("node:jobs"), data).status_code, 404)

    def test_unconfirmed_ai_not_claimed_by_node_worker(self):
        from django.core.management import call_command
        with patch("node.gemini.execute_confirmed") as execute:
            call_command("run_analysis_worker", worker_id="fixture", output=str(self.paths.artifacts_dir), once=True)
            execute.assert_not_called()
        self.assertEqual(AnalysisJob.objects.filter(executor="ai", status="pending").count(), 1)

    def test_jobs_get_is_read_only_and_cancel_is_audited(self):
        with patch("feedback.ai_stage_service.generate_stage", side_effect=AssertionError("GET must not compute")):
            response = self.client.get(reverse("node:jobs"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "最新資料時間")
        job = AnalysisJob.objects.get(executor="ai")
        self.client.post(reverse("node:jobs"), {"action": "cancel", "job": job.pk})
        job.refresh_from_db()
        self.assertEqual(job.status, "cancelled")
