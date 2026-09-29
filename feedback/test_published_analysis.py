from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch

from .test_utils import page_with_scripts
from .models import (
    AnalysisJob,
    ImprovementUpdate,
    Survey,
    SurveyAIAnalysisStage,
    SurveyAIReportSnapshot,
    SurveyAnalysisState,
)
from .published_analysis import (
    get_published_ai_pipeline_status,
    get_published_analysis_payload,
    is_published_ai_stage_current,
)


class PublishedAnalysisReadTests(TestCase):
    def setUp(self):
        self.manager = get_user_model().objects.create_user(
            username="published-manager",
            password="pass",
            role="manager",
        )
        self.survey = Survey.objects.create(title="Published fixture", slug="published-fixture")
        self.draft_id = "11111111-1111-4111-8111-111111111111"
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
                "improvement_drafts": [
                    {
                        "draft_id": self.draft_id,
                        "title": "Published draft",
                        "summary": "Review the published evidence.",
                        "related_category": "service",
                        "priority": "medium",
                        "rationale": "Published evidence supports a review.",
                        "acceptance_criteria": [],
                        "evidence_refs": ["E001"],
                        "evidence": [],
                        "data_limitations": [],
                    }
                ],
                "data_caveats": [],
                "_evidence_registry": {
                    "E001": {"id": "E001", "kind": "response_count", "value": 4},
                    "secret": "must-not-be-returned",
                },
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
                "improvement_drafts": [
                    {
                        "draft_id": self.draft_id,
                        "title": "Published draft",
                        "summary": "Review the published evidence.",
                        "priority": "medium",
                    }
                ],
                "data_caveats": [],
                "stage_id": self.ai_stage.pk,
                "model_name": "gemini-fixture",
            },
            publication_manifest={
                "statistics": {"input_version": 1, "config_version": 2, "pipeline_version": "pipeline-v1"},
                "text": {"input_version": 1, "config_version": 2, "pipeline_version": "pipeline-v1"},
                "ai": {
                    "snapshot_id": self.snapshot.pk,
                    "stage_id": self.ai_stage.pk,
                    "input_version": 0,
                    "config_version": 2,
                    "pipeline_version": "pipeline-v1",
                },
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
        self.assertEqual(payload["report"]["draft_states"], {})
        self.assertNotIn("must-not-be-returned", repr(payload))

    def test_current_published_drafts_have_manual_action_links(self):
        manifest = dict(self.state.publication_manifest)
        manifest["ai"] = {**manifest["ai"], "input_version": self.state.input_version}
        self.state.publication_manifest = manifest
        self.state.save(update_fields=("publication_manifest",))

        self.assertTrue(is_published_ai_stage_current(self.ai_stage))
        payload = get_published_ai_pipeline_status(self.survey)
        draft_state = payload["report"]["draft_states"][self.draft_id]
        self.assertFalse(draft_state["imported"])
        self.assertIn(f"/{self.ai_stage.pk}/{self.draft_id}/new/", draft_state["url"])

        improvement = ImprovementUpdate.objects.create(
            survey=self.survey,
            title="Imported draft",
            summary="Saved for follow-up.",
            source_ai_analysis_stage=self.ai_stage,
            source_ai_draft_id=self.draft_id,
        )
        payload = get_published_ai_pipeline_status(self.survey)
        draft_state = payload["report"]["draft_states"][self.draft_id]
        self.assertTrue(draft_state["imported"])
        self.assertIn(f"#improvement-{improvement.pk}", draft_state["url"])

    def test_published_draft_form_uses_version_pointer_without_scanning_answers(self):
        manifest = dict(self.state.publication_manifest)
        manifest["ai"] = {**manifest["ai"], "input_version": self.state.input_version}
        self.state.publication_manifest = manifest
        self.state.save(update_fields=("publication_manifest",))
        self.client.force_login(self.manager)
        url = reverse(
            "feedback:ai-stage-improvement-draft",
            args=[self.survey.slug, self.ai_stage.pk, self.draft_id],
        )

        with patch("feedback.ai_stage_service.is_stage_current", side_effect=AssertionError("must not scan answers")):
            response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Published draft")
        self.assertContains(response, "營運總覽")
        self.assertContains(response, "改善追蹤")
        self.assertEqual(response.context["active_section"], "feedback:improvement-list")
        self.assertEqual(len(response.context["dashboard_nav"]), 7)

    def test_dashboard_links_to_dedicated_analysis_page(self):
        self.client.force_login(self.manager)

        response = self.client.get(reverse("feedback:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "開啟營運分析")
        self.assertContains(response, reverse("feedback:analysis-operations"))
        self.assertNotContains(response, "data-ai-operations")

    def test_dedicated_analysis_page_only_reads_published_results(self):
        self.client.force_login(self.manager)

        response = self.client.get(reverse("feedback:analysis-operations"))

        page = page_with_scripts(response)

        self.assertEqual(response.status_code, 200)
        self.assertIn("網站唯讀展示", page)
        self.assertIn("重新讀取發布狀態", page)
        self.assertIn("data-ai-refresh", page)
        self.assertNotIn("data-ai-update", page)
        self.assertNotIn("data-snapshot-url", page)
        self.assertNotIn("產生第一份報告", page)
        self.assertNotIn("報告已過期，請重新產生後再帶入", page)
        self.assertIn("此為 AI 建議草稿，尚未建立改善追蹤項目", page)
        self.assertEqual(response.context["active_section"], "feedback:analysis-operations")
        self.assertEqual(
            [route for route, _, _ in response.context["dashboard_nav"][:3]],
            ["feedback:dashboard", "feedback:analysis-operations", "feedback:survey-manager"],
        )

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

    def test_stats_and_text_views_never_call_request_time_analysis(self):
        self.client.force_login(self.manager)
        with patch("feedback.local_service.build_stats_payload", side_effect=AssertionError("must not calculate")):
            response = self.client.get(reverse("feedback:stats-overview"), {"survey": self.survey.slug})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["charts"][0]["count"], 4)
        self.assertEqual(response.context["analysis_publication"]["snapshot_id"], self.snapshot.pk)
        self.assertContains(response, "符合目前資料版本")
        self.assertContains(response, "AI 結果仍為上一版本")

        with patch("feedback.local_service.build_text_analysis_payload", side_effect=AssertionError("must not calculate")):
            response = self.client.get(reverse("feedback:text-analysis"), {"survey": self.survey.slug})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["keywords"][0]["keyword"], "clean")
        self.assertEqual(response.context["analysis_publication"]["freshness"]["ai"], False)
        self.assertContains(response, "已發布結果")

    def test_ai_status_uses_published_state_and_sync_generation_is_not_routed(self):
        self.client.force_login(self.manager)
        with patch("feedback.ai_stage_service.get_pipeline_status", side_effect=AssertionError("must not scan")):
            response = self.client.get(reverse("feedback:ai-stage-status", args=[self.survey.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["report"]["content"]["executive_summary"], "Published AI summary")

        # Snapshot and Gemini generation run only in the local worker, never in a web request.
        for path in (
            f"/dashboard/ai-reports/{self.survey.slug}/snapshot/",
            f"/dashboard/ai-reports/{self.survey.slug}/snapshots/{self.snapshot.pk}/generate/",
            f"/dashboard/ai-reports/{self.survey.slug}/snapshots/{self.snapshot.pk}/stages/statistics/generate/",
        ):
            self.assertEqual(self.client.post(path).status_code, 404)
