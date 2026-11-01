import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from cloudapi.envelope import sha256_hex
from cloudapi.models import NodeDevice, SubmissionReceipt
from cloudapi.results import apply_upload
from cloudapi.tests.test_results import STAGE, content
from cloudapi.writes import assign_survey_to_node

from .models import ImprovementUpdate, Survey
from .published_analysis import get_published_ai_pipeline_status
from .test_utils import cloud_only

NODE_STAGE_ID = 77
DRAFT_ID = "22222222-2222-4222-8222-222222222222"


def ai_payload():
    return {
        "executive_summary": "等候時間是主要問題。",
        "combined_findings": [],
        "improvement_drafts": [
            {
                "draft_id": DRAFT_ID,
                "title": "縮短尖峰等候時間",
                "summary": "尖峰時段增加一名點餐人員。",
                "related_category": "服務",
                "priority": "high",
                "rationale": "等候時間與滿意度呈負相關。",
                "acceptance_criteria": ["尖峰平均等候低於 5 分鐘"],
                "evidence_refs": ["E001"],
                "data_limitations": ["樣本為模擬資料"],
                "evidence": [
                    {"id": "E001", "label": "等候時間提及", "value": 34, "unit": "occurrences"},
                    {"id": "test.test-1.statistic", "kind": "statistical_test", "metric_type": "statistic", "label": "等級相關係數 ρ",
                     "method_key": "spearman", "test_name": "Spearman 等級相關分析",
                     "variables": ["等候分鐘數", "整體滿意度"], "value": -0.7512, "sample_size": 12345},
                ],
            }
        ],
        "data_caveats": [],
        "stage_id": NODE_STAGE_ID,
        "generated_at": "2026-10-30T08:00:00+00:00",
        "model_name": "gemini-fixture",
    }


@cloud_only
class NodeDraftImportTests(TestCase):
    def setUp(self):
        self.node, _ = NodeDevice.issue("office")
        self.survey = assign_survey_to_node(Survey.objects.create(title="飲料店", slug="drinks"), self.node).survey
        Survey.objects.filter(pk=self.survey.pk).update(response_sequence=1)
        SubmissionReceipt.objects.create(
            submission_uuid=uuid.uuid4(), node=self.node, survey=self.survey, submitted_at="2026-10-30T07:00:00+00:00",
            definition_version=1, response_sequence=1, payload_hash="0" * 64, status="synced")
        self.upload(1, ai_current=True)
        self.manager = get_user_model().objects.create_user(username="m", password="x", role="manager")
        self.client.force_login(self.manager)
        self.url = reverse("feedback:node-ai-improvement-draft", args=[self.survey.slug, NODE_STAGE_ID, DRAFT_ID])

    def upload(self, sequence, *, ai_current):
        body = content(self.survey)
        body["stages"]["ai"] = {**STAGE, "current": ai_current, "model_name": "gemini-fixture"}
        body["ai_payload"] = ai_payload()
        apply_upload(self.node, publish_uuid=str(uuid.uuid4()), publish_sequence=sequence,
                     content_hash=sha256_hex(body), content=body)

    def form_data(self):
        return {"title": "縮短尖峰等候時間", "summary": "尖峰時段增加一名點餐人員。", "related_category": "服務",
                "status": "planned", "priority": "high"}

    def draft_state(self):
        return get_published_ai_pipeline_status(Survey.objects.get(pk=self.survey.pk))["report"]["draft_states"][DRAFT_ID]

    def test_current_node_draft_links_to_cloud_import(self):
        state = self.draft_state()
        self.assertFalse(state["imported"])
        self.assertEqual(state["url"], self.url)

    def test_form_is_prefilled_from_the_uploaded_draft(self):
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["form"].initial["title"], "縮短尖峰等候時間")
        self.assertIn("驗收方向：\n- 尖峰平均等候低於 5 分鐘", page.context["form"].initial["summary"])
        self.assertContains(page, "等候時間與滿意度呈負相關。")
        self.assertContains(page, "等候時間提及")

    def test_import_records_node_source_and_is_not_repeated(self):
        response = self.client.post(self.url, self.form_data())
        self.assertEqual(response.status_code, 302)
        improvement = ImprovementUpdate.objects.get()
        self.assertIsNone(improvement.source_ai_analysis_stage_id)
        self.assertEqual(improvement.source_ai_draft_id, DRAFT_ID)
        self.assertEqual(improvement.priority, "high")
        self.assertEqual(improvement.source_evidence_refs, ["E001"])
        self.assertEqual(improvement.source_ai_metadata["node_stage_id"], NODE_STAGE_ID)
        self.assertEqual(improvement.source_ai_metadata["evidence"][0]["label"], "等候時間提及")
        self.assertEqual(improvement.status_history.count(), 1)

        again = self.client.post(self.url, self.form_data())
        self.assertIn(f"#improvement-{improvement.pk}", again["Location"])
        self.assertEqual(ImprovementUpdate.objects.count(), 1)
        state = self.draft_state()
        self.assertTrue(state["imported"])
        self.assertIn(f"#improvement-{improvement.pk}", state["url"])

        detail = self.client.get(reverse("feedback:improvement-detail", args=[improvement.pk]))
        self.assertContains(detail, "AI 分析依據")
        self.assertContains(detail, "等候時間提及")

    def test_imported_item_lists_the_action_not_the_repeated_title(self):
        self.client.post(self.url, {**self.form_data(), "summary": "問題與目標：\n縮短尖峰等候時間\n\n建議行動：\n尖峰時段增加一名點餐人員。"})
        page = self.client.get(reverse("feedback:improvement-list") + f"?survey={self.survey.slug}")
        self.assertContains(page, "建議行動：")
        self.assertNotContains(page, "問題與目標：")

    def test_evidence_on_the_import_form_uses_report_formatting(self):
        page = self.client.get(self.url)
        self.assertContains(page, "-0.75")
        self.assertContains(page, "12,345")
        self.assertNotContains(page, "-0.7512")

    def test_outdated_or_unknown_drafts_are_refused(self):
        self.upload(2, ai_current=False)
        self.assertEqual(self.client.get(self.url).status_code, 409)
        self.assertEqual(self.client.post(self.url, self.form_data()).status_code, 409)
        self.upload(3, ai_current=True)
        other_stage = reverse("feedback:node-ai-improvement-draft", args=[self.survey.slug, 999, DRAFT_ID])
        self.assertEqual(self.client.get(other_stage).status_code, 409)
        unknown = reverse("feedback:node-ai-improvement-draft", args=[self.survey.slug, NODE_STAGE_ID, uuid.uuid4()])
        self.assertEqual(self.client.get(unknown).status_code, 404)
        self.assertFalse(ImprovementUpdate.objects.exists())

    def test_cloud_owned_survey_and_customers_cannot_use_the_node_route(self):
        cloud = Survey.objects.create(title="雲端", slug="cloud-owned")
        url = reverse("feedback:node-ai-improvement-draft", args=[cloud.slug, NODE_STAGE_ID, DRAFT_ID])
        self.assertEqual(self.client.get(url).status_code, 404)
        customer = get_user_model().objects.create_user(username="c", password="x", role="customer")
        self.client.force_login(customer)
        self.assertEqual(self.client.get(self.url).status_code, 403)


@override_settings(IS_NODE=True)
class NodeConsoleDraftImportTests(TestCase):
    """On the node, drafts are imported on the website instead (local items never reach the cloud)."""

    def setUp(self):
        from .test_published_analysis import PublishedAnalysisReadTests

        PublishedAnalysisReadTests.setUp(self)  # the shared published-stage fixture
        manifest = dict(self.state.publication_manifest)
        manifest["ai"] = {**manifest["ai"], "input_version": self.state.input_version}
        self.state.publication_manifest = manifest
        self.state.save(update_fields=("publication_manifest",))

    def test_node_console_points_to_the_website(self):
        report = get_published_ai_pipeline_status(self.survey)["report"]
        self.assertEqual(report["draft_states"], {})
        self.assertIn("網站", report["draft_import_hint"])
        self.client.force_login(self.manager)
        url = reverse("feedback:ai-stage-improvement-draft", args=[self.survey.slug, self.ai_stage.pk, self.draft_id])
        self.assertEqual(self.client.get(url).status_code, 404)
