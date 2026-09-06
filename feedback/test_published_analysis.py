from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch

from .models import AnalysisJob, Survey, SurveyAIAnalysisStage, SurveyAIReportSnapshot, SurveyAnalysisState
from .published_analysis import get_published_ai_pipeline_status, get_published_analysis_payload


@override_settings(ANALYSIS_READ_PUBLISHED_ONLY=True)
class PublishedAnalysisReadTests(TestCase):
    def setUp(self):
        self.manager = get_user_model().objects.create_user(
            username="published-manager",
            password="pass",
            role="manager",
        )
        self.survey = Survey.objects.create(title="Published fixture", slug="published-fixture")
        display = {
            "schema_version": "analysis-display-v1",
            "snapshot": {
                "response_count": 4,
                "analysis_coverage": 1,
                "source_latest_at": None,
                "generated_at": None,
                "model_name": "gemini-fixture",
            },
            "statistics": {
                "charts": [
                    {
                        "type": "numeric",
                        "question": {"title": "Rating", "kind": "scale", "data_type": "ordinal"},
                        "count": 4,
                        "counts": [{"value": "5", "total": 4, "percent": 100}],
                    }
                ],
                "question_analysis": [],
                "inferential_analysis": [],
            },
            "text_analysis": {
                "keywords": [{"keyword": "clean", "count": 4, "category": "room"}],
                "summary": {"total_answers": 4, "analyzed_answers": 4},
                "category_sentiments": [],
            },
        }
        self.snapshot = SurveyAIReportSnapshot.objects.create(
            survey=self.survey,
            data_fingerprint="f" * 64,
            snapshot_schema_version="1",
            prompt_version="fixture-v1",
            model_name="fixture-model",
            source_snapshot={
                "statistics": {},
                "text_analysis": {},
                "display_payload": display,
                "evidence_catalog": [{"private_marker": "must-not-be-returned"}],
            },
            status=SurveyAIReportSnapshot.Status.SNAPSHOT_READY,
            response_count=4,
        )
        self.ai_stage = SurveyAIAnalysisStage.objects.create(
            snapshot=self.snapshot,
            stage_type=SurveyAIAnalysisStage.StageType.SYNTHESIS,
            status=SurveyAIAnalysisStage.Status.SUCCEEDED,
            input_hash="a" * 64,
            schema_version="1",
            prompt_version="1",
            model_name="gemini-fixture",
            output_json={
                "executive_summary": "Published AI summary",
                "combined_findings": [],
                "improvement_drafts": [],
                "data_caveats": [],
                "_evidence_registry": {"secret": "must-not-be-returned"},
            },
            generated_at=timezone.now(),
        )
        self.state = SurveyAnalysisState.objects.create(
            survey=self.survey,
            input_version=1,
            config_version=2,
            pipeline_version="pipeline-v1",
            published_snapshot=self.snapshot,
            published_ai_stage=self.ai_stage,
            published_display_payload=display,
            published_ai_payload={
                "executive_summary": "Published AI summary",
                "combined_findings": [],
                "improvement_drafts": [],
                "data_caveats": [],
                "stage_id": self.ai_stage.pk,
                "model_name": "gemini-fixture",
            },
            publication_manifest={
                "statistics": {"input_version": 1, "config_version": 2, "pipeline_version": "pipeline-v1"},
                "text": {"input_version": 1, "config_version": 2, "pipeline_version": "pipeline-v1"},
                "ai": {"input_version": 0, "config_version": 2, "pipeline_version": "pipeline-v1"},
            },
            published_at=timezone.now(),
        )

    def test_service_reads_only_pointers_and_returns_finite_payload(self):
        with self.assertNumQueries(2):
            payload = get_published_analysis_payload(self.survey)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["statistics"]["charts"][0]["count"], 4)
        self.assertEqual(payload["text_analysis"]["keywords"][0]["keyword"], "clean")
        self.assertEqual(payload["ai"]["executive_summary"], "Published AI summary")
        self.assertNotIn("must-not-be-returned", repr(payload))
        self.assertEqual(payload["freshness"], {"statistics": True, "text": True, "ai": False})

    def test_dashboard_status_uses_finite_publication_contract(self):
        with self.assertNumQueries(2):
            payload = get_published_ai_pipeline_status(self.survey)
        self.assertEqual(payload["survey"]["valid_response_count"], 4)
        self.assertTrue(payload["freshness"]["base_is_current"])
        self.assertFalse(payload["freshness"]["is_current"])
        self.assertEqual(payload["workflow"]["mode"], "local_worker")
        self.assertEqual(payload["workflow"]["website_role"], "published_read_only")
        self.assertEqual(payload["report"]["content"]["executive_summary"], "Published AI summary")
        self.assertNotIn("must-not-be-returned", repr(payload))

    def test_dashboard_ai_module_only_reads_published_results(self):
        self.client.force_login(self.manager)

        response = self.client.get(reverse("feedback:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "網站唯讀展示")
        self.assertContains(response, "重新讀取發布狀態")
        self.assertContains(response, "data-ai-refresh")
        self.assertNotContains(response, "data-ai-update")
        self.assertNotContains(response, "data-snapshot-url")
        self.assertNotContains(response, "產生第一份報告")
        self.assertNotContains(response, "報告已過期，請重新產生後再帶入")
        self.assertContains(response, "此為 AI 建議草稿，尚未建立改善追蹤項目")

    def test_stale_ai_keeps_its_own_snapshot_metadata(self):
        newer_snapshot = SurveyAIReportSnapshot.objects.create(
            survey=self.survey,
            data_fingerprint="b" * 64,
            snapshot_schema_version="1",
            prompt_version="fixture-v1",
            model_name="fixture-model",
            source_snapshot={},
            status=SurveyAIReportSnapshot.Status.SNAPSHOT_READY,
            response_count=9,
        )
        newer_display = {
            **self.state.published_display_payload,
            "snapshot": {
                **self.state.published_display_payload["snapshot"],
                "response_count": 9,
            },
        }
        self.state.published_snapshot = newer_snapshot
        self.state.published_display_payload = newer_display
        self.state.save(update_fields=("published_snapshot", "published_display_payload"))

        payload = get_published_ai_pipeline_status(self.survey)

        self.assertEqual(payload["survey"]["response_count"], 9)
        self.assertEqual(payload["report"]["snapshot_id"], self.snapshot.pk)
        self.assertEqual(payload["report"]["response_count"], 4)

    def test_stats_and_text_views_do_not_call_request_time_analysis_when_enabled(self):
        self.client.force_login(self.manager)
        with patch("feedback.views.local_service.get_stats_payload", side_effect=AssertionError("must not calculate")):
            response = self.client.get(reverse("feedback:stats-overview"), {"survey": self.survey.slug})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["charts"][0]["count"], 4)
        self.assertEqual(response.context["analysis_publication"]["snapshot_id"], self.snapshot.pk)
        self.assertContains(response, "符合目前資料版本")
        self.assertContains(response, "AI 結果仍為上一版本")

        with patch("feedback.views.local_service.get_text_analysis_payload", side_effect=AssertionError("must not calculate")):
            response = self.client.get(reverse("feedback:text-analysis"), {"survey": self.survey.slug})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["keywords"][0]["keyword"], "clean")
        self.assertEqual(response.context["analysis_publication"]["freshness"]["ai"], False)
        self.assertContains(response, "已發布結果")

    def test_ai_status_and_update_use_published_state_and_background_queue(self):
        self.client.force_login(self.manager)
        with patch("feedback.views.get_pipeline_status", side_effect=AssertionError("must not scan")):
            response = self.client.get(reverse("feedback:ai-stage-status", args=[self.survey.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["report"]["content"]["executive_summary"], "Published AI summary")

        with patch("feedback.views.build_or_reuse_snapshot", side_effect=AssertionError("must not build")):
            response = self.client.post(reverse("feedback:ai-report-snapshot", args=[self.survey.slug]))
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["queued"])
        self.assertTrue(
            AnalysisJob.objects.filter(
                survey=self.survey,
                executor=AnalysisJob.Executor.DETERMINISTIC,
                status=AnalysisJob.Status.PENDING,
            ).exists()
        )
