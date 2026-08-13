import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.core.exceptions import ValidationError
from django.test import Client, override_settings
from django.urls import reverse

from . import ai_synthesis_service
from .ai_report_service import validate_report_payload
from .ai_snapshot_service import calculate_data_fingerprint
from .ai_stage_service import (
    StageError,
    build_stage_input,
    generate_stage,
    is_stage_current,
    prepare_stage,
    stage_input_hash,
)
from .models import (
    FeedbackSubmission,
    ImprovementDispatch,
    ImprovementUpdate,
    SurveyAIAnalysisStage,
    SurveyAIReportSnapshot,
)
from .tests import AIReportTestCase, provider_report, source_snapshot


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AIStageModelAndNotificationTests(AIReportTestCase):
    def make_stage(self, *, revision=1, stage_type=SurveyAIAnalysisStage.StageType.SYNTHESIS):
        snapshot = self.make_snapshot(fingerprint=str(revision) * 64)
        return SurveyAIAnalysisStage.objects.create(
            snapshot=snapshot,
            stage_type=stage_type,
            status=SurveyAIAnalysisStage.Status.SUCCEEDED,
            input_hash="a" * 64,
            schema_version="1",
            prompt_version="1",
            model_name="gemini-2.5-flash",
            revision=revision,
            output_json={"improvement_drafts": []},
        )

    def add_notification_recipient(self):
        self.customer.notification_opt_in = True
        self.customer.email = "customer@example.com"
        self.customer.save(update_fields=["notification_opt_in", "email"])
        return FeedbackSubmission.objects.create(
            survey=self.survey,
            user=self.customer,
            respondent_name="Customer",
            respondent_email=self.customer.email,
            consent_follow_up=True,
        )

    def test_manual_improvement_keeps_ai_provenance_nullable(self):
        improvement = ImprovementUpdate.objects.create(
            survey=self.survey,
            title="手動改善",
            summary="由管理者建立。",
        )
        self.assertIsNone(improvement.source_ai_analysis_stage)
        self.assertIsNone(improvement.source_ai_draft_id)
        self.assertIsNone(improvement.source_evidence_refs)
        self.assertIsNone(improvement.source_ai_metadata)

    def test_stage_revision_identity_is_unique(self):
        stage = self.make_stage()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SurveyAIAnalysisStage.objects.create(
                    snapshot=stage.snapshot,
                    stage_type=stage.stage_type,
                    status=SurveyAIAnalysisStage.Status.FAILED,
                    input_hash=stage.input_hash,
                    schema_version=stage.schema_version,
                    prompt_version=stage.prompt_version,
                    model_name=stage.model_name,
                    revision=stage.revision,
                )
        self.assertTrue(SurveyAIAnalysisStage.objects.filter(pk=stage.pk).exists())

    def test_terminal_stage_cannot_be_overwritten(self):
        stage = self.make_stage()
        stage.error_code = "tampered"
        with self.assertRaises(ValidationError):
            stage.save(update_fields=["error_code"])
        stage.refresh_from_db()
        self.assertEqual(stage.error_code, "")

    def test_ai_stage_and_draft_pair_is_unique(self):
        stage = self.make_stage()
        ImprovementUpdate.objects.create(
            survey=self.survey,
            title="第一筆",
            summary="第一筆",
            source_ai_analysis_stage=stage,
            source_ai_draft_id="draft-one",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ImprovementUpdate.objects.create(
                    survey=self.survey,
                    title="重複",
                    summary="重複",
                    source_ai_analysis_stage=stage,
                    source_ai_draft_id="draft-one",
                )
        self.assertEqual(ImprovementUpdate.objects.count(), 1)

    def test_deleting_stage_keeps_improvement_audit_metadata(self):
        stage = self.make_stage()
        improvement = ImprovementUpdate.objects.create(
            survey=self.survey,
            title="AI 改善",
            summary="AI 改善內容",
            source_ai_analysis_stage=stage,
            source_ai_draft_id="draft-one",
            source_evidence_refs=["stats.wait.mean"],
            source_ai_metadata={"priority": "high", "prompt_version": "1"},
        )
        stage.delete()
        improvement.refresh_from_db()
        self.assertIsNone(improvement.source_ai_analysis_stage)
        self.assertEqual(improvement.source_ai_draft_id, "draft-one")
        self.assertEqual(improvement.source_evidence_refs, ["stats.wait.mean"])
        self.assertEqual(improvement.source_ai_metadata["priority"], "high")

    @patch("feedback.views.send_mail")
    def test_send_global_notice_false_creates_no_dispatch_or_email(self, send_mail):
        self.add_notification_recipient()
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("feedback:improvement-create", args=[self.survey.slug]),
            {
                "title": "不通知的改善",
                "summary": "僅建立追蹤項目。",
                "related_category": "流程",
            },
        )
        self.assertEqual(response.status_code, 302)
        improvement = ImprovementUpdate.objects.get()
        self.assertFalse(improvement.send_global_notice)
        self.assertEqual(ImprovementDispatch.objects.count(), 0)
        send_mail.assert_not_called()

    @patch("feedback.views.send_mail")
    def test_send_global_notice_true_keeps_manual_notification_flow(self, send_mail):
        self.add_notification_recipient()
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("feedback:improvement-create", args=[self.survey.slug]),
            {
                "title": "通知改善",
                "summary": "管理者主動確認通知。",
                "related_category": "",
                "send_global_notice": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        improvement = ImprovementUpdate.objects.get()
        self.assertTrue(improvement.send_global_notice)
        self.assertEqual(ImprovementDispatch.objects.count(), 1)
        send_mail.assert_called_once()


def finding_payload(evidence_ref="stats.wait.mean"):
    return {
        "title": "聚合資料顯示需關注",
        "rationale": "此發現只根據已保存的聚合證據。",
        "evidence_refs": [evidence_ref],
        "data_limitations": [],
    }


def statistics_payload():
    return {
        "descriptive_statistics": [finding_payload()],
        "categorical_distributions": [],
        "group_comparisons": [],
        "correlations": [],
        "inferential_tests": [],
        "statistical_caveats": [],
    }


def text_payload():
    return {
        "keyword_findings": [],
        "category_sentiments": [],
        "positive_signals": [],
        "negative_signals": [],
        "text_coverage": [],
        "text_caveats": [],
    }


def synthesis_payload():
    return {
        "executive_summary": "整體應優先改善等候流程。",
        "combined_findings": [
            {
                "title": "等候流程值得優先處理",
                "source_stages": ["statistics"],
                "priority": "high",
                "rationale": "統計聚合證據支持此改善方向。",
                "evidence_refs": ["stats.wait.mean"],
                "data_limitations": [],
            }
        ],
        "improvement_drafts": [
            {
                "title": "改善等候流程",
                "summary": "檢視尖峰流程並安排改善。",
                "related_category": "等候時間",
                "priority": "high",
                "rationale": "此草稿引用已驗證的統計證據。",
                "acceptance_criteria": ["建立尖峰流程檢核方式"],
                "evidence_refs": ["stats.wait.mean"],
                "data_limitations": [],
            },
            {
                "title": "建立持續追蹤",
                "summary": "定期檢視等候改善趨勢。",
                "related_category": "流程",
                "priority": "medium",
                "rationale": "需要持續觀察聚合資料。",
                "acceptance_criteria": ["建立定期檢視節奏"],
                "evidence_refs": ["stats.wait.mean"],
                "data_limitations": [],
            },
        ],
        "data_caveats": [],
    }


def provider_response(payload):
    return SimpleNamespace(
        text=json.dumps(payload, ensure_ascii=False),
        usage_metadata=SimpleNamespace(
            prompt_token_count=100,
            candidates_token_count=50,
            thoughts_token_count=10,
            total_token_count=160,
        ),
    )


class AIStageServiceTests(AIReportTestCase):
    def setUp(self):
        super().setUp()
        self.add_responses(count=3)
        self.snapshot = self.make_snapshot(fingerprint=calculate_data_fingerprint(self.survey).value)

    def create_success_stage(self, stage_type, output_json, *, snapshot=None):
        snapshot = snapshot or self.snapshot
        input_hash = stage_input_hash(snapshot, stage_type)
        module_versions = {
            "statistics": ("2", "2"),
            "text": ("2", "2"),
            "synthesis": ("3", "2"),
        }
        schema_version, prompt_version = module_versions[stage_type]
        revision = snapshot.analysis_stages.filter(stage_type=stage_type).count() + 1
        return SurveyAIAnalysisStage.objects.create(
            snapshot=snapshot,
            stage_type=stage_type,
            status=SurveyAIAnalysisStage.Status.SUCCEEDED,
            input_hash=input_hash,
            schema_version=schema_version,
            prompt_version=prompt_version,
            model_name="gemini-2.5-flash",
            revision=revision,
            input_manifest={
                "analysis_source_hash": calculate_data_fingerprint(
                    snapshot.survey,
                    include_improvements=False,
                ).value,
            },
            output_json=output_json,
        )

    def create_upstream_stages(self, *, snapshot=None):
        snapshot = snapshot or self.snapshot
        registry = {
            "stats.wait.mean": source_snapshot(self.survey.slug)["evidence_catalog"][0],
        }
        statistics = self.create_success_stage(
            SurveyAIAnalysisStage.StageType.STATISTICS,
            {**statistics_payload(), "_evidence_registry": registry},
            snapshot=snapshot,
        )
        text = self.create_success_stage(
            SurveyAIAnalysisStage.StageType.TEXT,
            {**text_payload(), "_evidence_registry": {}},
            snapshot=snapshot,
        )
        return statistics, text

    def test_stage_inputs_are_isolated_and_never_include_answer_value(self):
        stats_input, _ = build_stage_input(self.snapshot, SurveyAIAnalysisStage.StageType.STATISTICS)
        text_input, _ = build_stage_input(self.snapshot, SurveyAIAnalysisStage.StageType.TEXT)
        serialized = json.dumps([stats_input, text_input], ensure_ascii=False)
        self.assertNotIn("回饋0", serialized)
        self.assertNotIn("Answer.value", serialized)
        self.assertIn("statistics", stats_input)
        self.assertNotIn("text_analysis", stats_input)
        self.assertIn("text_analysis", text_input)
        self.assertNotIn("statistics", text_input)

    @patch("feedback.ai_stage_service.create_gemini_client")
    def test_statistics_and_text_generate_independently(self, client_factory):
        client = client_factory.return_value
        client.models.generate_content.side_effect = [
            provider_response(statistics_payload()),
            provider_response(text_payload()),
        ]
        statistics = generate_stage(self.snapshot, SurveyAIAnalysisStage.StageType.STATISTICS)
        text = generate_stage(self.snapshot, SurveyAIAnalysisStage.StageType.TEXT)
        self.assertEqual(statistics.status, SurveyAIAnalysisStage.Status.SUCCEEDED)
        self.assertEqual(text.status, SurveyAIAnalysisStage.Status.SUCCEEDED)
        self.assertEqual(client.models.generate_content.call_count, 2)
        self.assertIn("stats.wait.mean", statistics.output_json["_evidence_registry"])
        statistics_schema = client.models.generate_content.call_args_list[0].kwargs["config"].response_schema
        evidence_items = statistics_schema["properties"]["descriptive_statistics"]["items"]["properties"]["evidence_refs"]["items"]
        self.assertNotIn("enum", evidence_items)

    @override_settings(AI_REPORT_REQUEST_INTERVAL_SECONDS=0)
    @patch("feedback.ai_stage_service.create_gemini_client")
    def test_schema_failure_retries_once_with_compact_profile(self, client_factory):
        invalid = statistics_payload()
        invalid["descriptive_statistics"][0]["rationale"] = "共有 103 份回覆。"
        client_factory.return_value.models.generate_content.side_effect = [
            provider_response(invalid),
            provider_response(statistics_payload()),
        ]

        stage = generate_stage(self.snapshot, SurveyAIAnalysisStage.StageType.STATISTICS)

        self.assertEqual(stage.status, SurveyAIAnalysisStage.Status.SUCCEEDED)
        self.assertEqual(stage.token_metrics["generation_profile"], "compact")
        self.assertEqual(stage.token_metrics["retry_count"], 1)
        self.assertEqual(stage.token_metrics["attempts"][0]["validation_reason"], "invalid_text")
        self.assertEqual(client_factory.return_value.models.generate_content.call_count, 2)

    @override_settings(AI_REPORT_REQUEST_INTERVAL_SECONDS=0)
    @patch("feedback.ai_stage_service.create_gemini_client")
    def test_authentication_failure_does_not_retry(self, client_factory):
        provider_error = RuntimeError("provider rejected request")
        provider_error.code = 401
        client_factory.return_value.models.generate_content.side_effect = provider_error

        with self.assertRaises(StageError) as raised:
            generate_stage(self.snapshot, SurveyAIAnalysisStage.StageType.STATISTICS)

        self.assertEqual(raised.exception.error_code, "authentication_error")
        self.assertEqual(client_factory.return_value.models.generate_content.call_count, 1)
        stage = self.snapshot.analysis_stages.filter(stage_type="statistics").latest("id")
        self.assertEqual(stage.status, SurveyAIAnalysisStage.Status.FAILED)
        self.assertEqual(stage.token_metrics["retry_count"], 0)

    @patch("feedback.ai_stage_service.create_gemini_client")
    def test_synthesis_uses_only_validated_upstream_outputs_and_backend_uuid(self, client_factory):
        self.create_upstream_stages()
        client_factory.return_value.models.generate_content.return_value = provider_response(synthesis_payload())
        synthesis = generate_stage(self.snapshot, SurveyAIAnalysisStage.StageType.SYNTHESIS)
        call = client_factory.return_value.models.generate_content.call_args
        sent_input = json.loads(call.kwargs["contents"])
        self.assertEqual(set(sent_input), {"data_scope", "statistics_analysis", "text_analysis", "existing_improvements", "upstream_stage_ids"})
        self.assertNotIn("source_snapshot", sent_input)
        schema = call.kwargs["config"].response_schema
        allowed_refs = schema["properties"]["improvement_drafts"]["items"]["properties"]["evidence_refs"]["items"]["enum"]
        self.assertEqual(allowed_refs, ["stats.wait.mean"])
        draft_id = synthesis.output_json["improvement_drafts"][0]["draft_id"]
        self.assertEqual(len(draft_id), 36)
        self.assertNotIn("draft_id", synthesis_payload()["improvement_drafts"][0])
        self.assertNotIn("survey_slug", sent_input)

    @override_settings(AI_REPORT_REQUEST_INTERVAL_SECONDS=0)
    @patch("feedback.ai_stage_service.create_gemini_client")
    def test_synthesis_deduplicates_valid_evidence_refs(self, client_factory):
        self.create_upstream_stages()
        payload = synthesis_payload()
        payload["combined_findings"][0]["evidence_refs"] = [
            "stats.wait.mean",
            "stats.wait.mean",
        ]
        client_factory.return_value.models.generate_content.return_value = provider_response(payload)

        stage = generate_stage(self.snapshot, SurveyAIAnalysisStage.StageType.SYNTHESIS)

        self.assertEqual(
            stage.output_json["combined_findings"][0]["evidence_refs"],
            ["stats.wait.mean"],
        )

    def test_synthesis_accepts_numeric_evidence_ids(self):
        evidence_id = "test.test-1.p_value"
        payload = synthesis_payload()
        payload["combined_findings"][0]["evidence_refs"] = [evidence_id]
        for draft in payload["improvement_drafts"]:
            draft["evidence_refs"] = [evidence_id]

        validated = ai_synthesis_service.validate_output(
            payload,
            {evidence_id: {"id": evidence_id, "kind": "statistical_test"}},
            "a" * 64,
        )

        self.assertEqual(validated["combined_findings"][0]["evidence_refs"], [evidence_id])

    @override_settings(AI_REPORT_REQUEST_INTERVAL_SECONDS=0)
    def test_synthesis_rejects_unknown_evidence_ref(self):
        self.create_upstream_stages()
        payload = synthesis_payload()
        payload["improvement_drafts"][0]["evidence_refs"] = ["missing.ref"]
        with patch("feedback.ai_stage_service.create_gemini_client") as client_factory:
            client_factory.return_value.models.generate_content.return_value = provider_response(payload)
            with self.assertRaises(StageError) as raised:
                generate_stage(self.snapshot, SurveyAIAnalysisStage.StageType.SYNTHESIS)
        self.assertEqual(raised.exception.error_code, "schema_invalid")
        stage = self.snapshot.analysis_stages.filter(stage_type="synthesis").latest("id")
        self.assertEqual(stage.status, SurveyAIAnalysisStage.Status.FAILED)
        self.assertEqual(stage.token_metrics["retry_count"], 1)
        self.assertEqual(
            [attempt["validation_reason"] for attempt in stage.token_metrics["attempts"]],
            ["invalid_evidence_refs", "invalid_evidence_refs"],
        )
        self.assertEqual(client_factory.return_value.models.generate_content.call_count, 2)

    def test_cross_snapshot_cache_creates_reused_row_for_current_snapshot(self):
        original = self.create_success_stage(
            SurveyAIAnalysisStage.StageType.STATISTICS,
            {**statistics_payload(), "_evidence_registry": {}},
        )
        second = self.make_snapshot(fingerprint="b" * 64)
        second.source_snapshot = source_snapshot(self.survey.slug)
        second.save(update_fields=["source_snapshot"])
        reused, _, _, cache_hit = prepare_stage(second, SurveyAIAnalysisStage.StageType.STATISTICS)
        self.assertTrue(cache_hit)
        self.assertEqual(reused.snapshot, second)
        self.assertEqual(reused.reused_from, original)
        self.assertEqual(reused.status, SurveyAIAnalysisStage.Status.SUCCEEDED)

    @patch("feedback.ai_stage_service.create_gemini_client")
    def test_terminal_stage_rerun_creates_new_revision(self, client_factory):
        first = self.create_success_stage(
            SurveyAIAnalysisStage.StageType.STATISTICS,
            {**statistics_payload(), "_evidence_registry": {}},
        )
        client_factory.return_value.models.generate_content.return_value = provider_response(statistics_payload())
        second = generate_stage(self.snapshot, SurveyAIAnalysisStage.StageType.STATISTICS, force=True)
        first.refresh_from_db()
        self.assertEqual(first.status, SurveyAIAnalysisStage.Status.SUCCEEDED)
        self.assertEqual(second.revision, first.revision + 1)
        self.assertNotEqual(second.pk, first.pk)

    def test_synthesis_freshness_ignores_own_imports_but_not_manual_changes(self):
        self.create_upstream_stages()
        synthesis = self.create_success_stage(
            SurveyAIAnalysisStage.StageType.SYNTHESIS,
            {**synthesis_payload(), "_evidence_registry": {}},
        )
        ImprovementUpdate.objects.create(
            survey=self.survey,
            title="由目前 stage 匯入",
            summary="同一 stage 的第一份草稿。",
            source_ai_analysis_stage=synthesis,
            source_ai_draft_id="own-draft",
        )
        self.assertTrue(is_stage_current(synthesis))
        ImprovementUpdate.objects.create(survey=self.survey, title="手動改善", summary="外部變更。")
        self.assertFalse(is_stage_current(synthesis))

    def test_next_synthesis_hash_includes_previous_stage_import(self):
        self.create_upstream_stages()
        synthesis = self.create_success_stage(
            SurveyAIAnalysisStage.StageType.SYNTHESIS,
            {**synthesis_payload(), "_evidence_registry": {}},
        )
        old_hash = synthesis.input_hash
        ImprovementUpdate.objects.create(
            survey=self.survey,
            title="上一版匯入",
            summary="下一版應納入摘要。",
            source_ai_analysis_stage=synthesis,
            source_ai_draft_id="old-draft",
        )
        new_hash = stage_input_hash(self.snapshot, SurveyAIAnalysisStage.StageType.SYNTHESIS)
        self.assertNotEqual(new_hash, old_hash)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AIStageDraftImportTests(AIReportTestCase):
    def setUp(self):
        super().setUp()
        self.add_responses(count=3)
        self.snapshot = self.make_snapshot(fingerprint=calculate_data_fingerprint(self.survey).value)
        with patch("feedback.ai_stage_service.create_gemini_client") as client_factory:
            client_factory.return_value.models.generate_content.side_effect = [
                provider_response(statistics_payload()),
                provider_response(text_payload()),
                provider_response(synthesis_payload()),
            ]
            generate_stage(self.snapshot, SurveyAIAnalysisStage.StageType.STATISTICS)
            generate_stage(self.snapshot, SurveyAIAnalysisStage.StageType.TEXT)
            self.stage = generate_stage(self.snapshot, SurveyAIAnalysisStage.StageType.SYNTHESIS)
        self.drafts = self.stage.output_json["improvement_drafts"]

    def draft_url(self, draft=None, *, survey=None, stage=None):
        draft = draft or self.drafts[0]
        survey = survey or self.survey
        stage = stage or self.stage
        return reverse(
            "feedback:ai-stage-improvement-draft",
            args=[survey.slug, stage.pk, draft["draft_id"]],
        )

    def post_draft(self, draft=None, **extra):
        draft = draft or self.drafts[0]
        payload = {
            "title": f"管理者確認：{draft['title']}",
            "summary": "管理者可編輯的確認內容。",
            "related_category": draft["related_category"],
            **extra,
        }
        return self.client.post(self.draft_url(draft), payload)

    def test_get_only_prefills_and_displays_backend_evidence(self):
        self.client.force_login(self.manager)
        response = self.client.get(self.draft_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "驗收方向")
        self.assertContains(response, "平均等候時間")
        self.assertContains(response, "優先程度：高優先")
        self.assertNotContains(response, "優先程度：high")
        self.assertContains(response, "目前最新，可帶入")
        self.assertEqual(ImprovementUpdate.objects.count(), 0)

    def test_draft_preview_uses_formatted_statistical_evidence(self):
        output = self.stage.output_json
        output["_evidence_registry"]["stats.wait.mean"] = {
            "id": "stats.wait.mean",
            "kind": "statistical_test",
            "label": "Spearman 等級相關分析 p value",
            "value": 0,
            "sample_size": 46,
            "metric_type": "p_value",
            "method_key": "spearman",
            "test_name": "Spearman 等級相關分析",
            "variables": ["等待時間感受", "整體滿意度"],
        }
        SurveyAIAnalysisStage.objects.filter(pk=self.stage.pk).update(output_json=output)
        self.stage.refresh_from_db()
        self.client.force_login(self.manager)
        response = self.client.get(self.draft_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "等待時間感受 × 整體滿意度")
        self.assertContains(response, "p &lt; 0.001")
        self.assertNotContains(response, "p value：0.0")

    @patch("feedback.views.send_mail")
    def test_post_saves_provenance_metadata_and_no_notice_by_default(self, send_mail):
        self.client.force_login(self.manager)
        response = self.post_draft(
            source_ai_analysis_stage="999",
            source_ai_draft_id="tampered",
            source_evidence_refs='["tampered"]',
            source_ai_metadata='{"priority":"low"}',
        )
        improvement = ImprovementUpdate.objects.get()
        self.assertRedirects(
            response,
            f"{reverse('feedback:improvement-list')}?survey={self.survey.slug}#improvement-{improvement.pk}",
            fetch_redirect_response=False,
        )
        self.assertEqual(improvement.source_ai_analysis_stage, self.stage)
        self.assertEqual(improvement.source_ai_draft_id, self.drafts[0]["draft_id"])
        self.assertEqual(improvement.source_evidence_refs, ["stats.wait.mean"])
        self.assertEqual(improvement.source_ai_metadata["priority"], "high")
        self.assertEqual(improvement.source_ai_metadata["acceptance_criteria"], ["建立尖峰流程檢核方式"])
        self.assertFalse(improvement.send_global_notice)
        self.assertEqual(ImprovementDispatch.objects.count(), 0)
        send_mail.assert_not_called()

    def test_two_drafts_from_same_stage_can_be_imported_in_sequence(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.post_draft(self.drafts[0]).status_code, 302)
        self.stage.refresh_from_db()
        self.assertTrue(is_stage_current(self.stage))
        self.assertEqual(self.post_draft(self.drafts[1]).status_code, 302)
        self.assertEqual(ImprovementUpdate.objects.count(), 2)
        self.assertTrue(is_stage_current(self.stage))

    def test_duplicate_post_redirects_to_existing_improvement(self):
        self.client.force_login(self.manager)
        first = self.post_draft()
        improvement = ImprovementUpdate.objects.get()
        second = self.post_draft()
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(ImprovementUpdate.objects.count(), 1)
        self.assertIn(f"#improvement-{improvement.pk}", second.url)

    def test_other_survey_stage_cannot_be_imported(self):
        other = self.survey.__class__.objects.create(title="其他問卷", slug="other-survey")
        self.client.force_login(self.manager)
        response = self.client.get(self.draft_url(survey=other))
        self.assertEqual(response.status_code, 404)

    def test_non_manager_is_rejected(self):
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(self.draft_url()).status_code, 403)

    def test_stale_synthesis_is_strictly_blocked(self):
        ImprovementUpdate.objects.create(survey=self.survey, title="外部改善", summary="使 synthesis 過期。")
        self.client.force_login(self.manager)
        response = self.client.get(self.draft_url())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(ImprovementUpdate.objects.count(), 1)

    def test_failed_or_generating_stage_is_not_importable(self):
        failed = SurveyAIAnalysisStage.objects.create(
            snapshot=self.snapshot,
            stage_type=SurveyAIAnalysisStage.StageType.SYNTHESIS,
            status=SurveyAIAnalysisStage.Status.FAILED,
            input_hash="f" * 64,
            schema_version="1",
            prompt_version="1",
            model_name="gemini-2.5-flash",
            revision=self.stage.revision + 1,
        )
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(self.draft_url(stage=failed)).status_code, 404)

    def test_invalid_saved_evidence_ref_is_blocked(self):
        report = self.stage.output_json
        report["improvement_drafts"][0]["evidence_refs"] = ["missing.ref"]
        SurveyAIAnalysisStage.objects.filter(pk=self.stage.pk).update(output_json=report)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(self.draft_url()).status_code, 409)

    def test_post_requires_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.manager)
        response = csrf_client.post(
            self.draft_url(),
            {"title": "確認", "summary": "確認", "related_category": "流程"},
        )
        self.assertEqual(response.status_code, 403)


class AIStageDashboardTests(AIReportTestCase):
    def setUp(self):
        super().setUp()
        self.add_responses(count=3)
        self.snapshot = self.make_snapshot(fingerprint=calculate_data_fingerprint(self.survey).value)
        self.client.force_login(self.manager)

    def status_url(self):
        return reverse("feedback:ai-stage-status", args=[self.survey.slug])

    def generate_url(self, stage_type):
        return reverse(
            "feedback:ai-stage-generate",
            args=[self.survey.slug, self.snapshot.pk, stage_type],
        )

    def test_dashboard_uses_stage_status_endpoint_and_shows_three_stage_shell(self):
        with patch("feedback.views.service_client.get_dashboard", return_value={}):
            response = self.client.get(reverse("feedback:dashboard"))
        self.assertContains(response, self.status_url())
        self.assertContains(response, "AI 正在分析統計資料")
        self.assertContains(response, "AI 正在整理文字洞察")
        self.assertContains(response, "AI 正在產生營運摘要與改善草稿")
        self.assertContains(response, "const status = freshness.is_current")
        self.assertNotContains(response, "const status = report.is_current")
        self.assertContains(response, "else {\n            clearError();")
        self.assertContains(response, 'class="ai-report-spinner"')
        self.assertContains(response, "progressText.textContent = message")
        self.assertContains(response, "function evidenceAnalysisKey(row)")
        self.assertContains(response, "group.includes(row)")
        self.assertContains(response, "Spearman ρ =")
        self.assertContains(response, "單因子變異數分析")
        self.assertContains(response, "ai-card-section-label', '分析依據")
        self.assertContains(response, "ai-card-section-label', '資料限制")
        self.assertContains(response, "button button-ghost button-small ai-card-action")
        self.assertContains(response, "['生成時間', formatDate(report.generated_at)]")
        self.assertNotContains(response, "['來源', report.cache_hit")
        self.assertContains(response, "high: '高優先'")
        self.assertContains(response, "medium: '中優先'")
        self.assertContains(response, "low: '低優先'")
        self.assertContains(response, "/static/css/app.css?v=20260814-ai4")

    def test_ai_report_css_has_balanced_desktop_and_single_column_rules(self):
        css_path = Path(__file__).resolve().parents[1] / "static" / "css" / "app.css"
        css = css_path.read_text(encoding="utf-8")
        self.assertIn("width: min(100%, 1560px);", css)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr));", css)
        self.assertIn(".ai-finding-grid > :last-child:nth-child(odd)", css)
        self.assertIn("width: calc((100% - 16px) / 2);", css)
        self.assertIn("@media (max-width: 1100px)", css)
        self.assertIn("grid-template-columns: 1fr;", css)
        self.assertIn("flex-direction: column;", css)
        self.assertIn("margin-top: auto !important;", css)
        self.assertIn(".manager-sidebar,\n    .manager-main {\n        width: 100%;\n        min-width: 0;", css)
        self.assertIn("grid-template-columns: minmax(0, 1fr);", css)
        self.assertIn(".dashboard-support-grid .trend-bar", css)

    def test_text_endpoint_requires_statistics_first(self):
        response = self.client.post(self.generate_url(SurveyAIAnalysisStage.StageType.TEXT))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error_code"], "upstream_incomplete")

    @patch("feedback.ai_stage_service.create_gemini_client")
    def test_endpoints_run_in_sequence_and_return_staged_report(self, client_factory):
        client_factory.return_value.models.generate_content.side_effect = [
            provider_response(statistics_payload()),
            provider_response(text_payload()),
            provider_response(synthesis_payload()),
        ]
        for stage_type in (
            SurveyAIAnalysisStage.StageType.STATISTICS,
            SurveyAIAnalysisStage.StageType.TEXT,
            SurveyAIAnalysisStage.StageType.SYNTHESIS,
        ):
            response = self.client.post(self.generate_url(stage_type))
            self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["freshness"]["is_current"])
        self.assertEqual(payload["report"]["report_source"], "staged")
        self.assertEqual(payload["report"]["generation_profile"], "standard")
        self.assertEqual(payload["stages"]["statistics"]["status"], "succeeded")
        draft_state = next(iter(payload["report"]["draft_states"].values()))
        self.assertFalse(draft_state["imported"])
        self.assertIn("/dashboard/improvements/", draft_state["url"])

    def test_legacy_success_is_returned_as_fallback_before_pipeline_completes(self):
        self.snapshot.status = SurveyAIReportSnapshot.Status.SUCCEEDED
        self.snapshot.ai_report = validate_report_payload(
            provider_report(self.survey.slug),
            self.snapshot.source_snapshot,
        )
        self.snapshot.generated_at = self.snapshot.created_at
        self.snapshot.save(update_fields=["status", "ai_report", "generated_at"])
        response = self.client.get(self.status_url())
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["report"]["report_source"], "legacy")
        self.assertFalse(payload["freshness"]["is_current"])
        self.assertTrue(payload["freshness"]["latest_analysis_incomplete"])

    def test_failed_stage_does_not_replace_legacy_success(self):
        self.snapshot.status = SurveyAIReportSnapshot.Status.SUCCEEDED
        self.snapshot.ai_report = validate_report_payload(
            provider_report(self.survey.slug),
            self.snapshot.source_snapshot,
        )
        self.snapshot.generated_at = self.snapshot.created_at
        self.snapshot.save(update_fields=["status", "ai_report", "generated_at"])
        SurveyAIAnalysisStage.objects.create(
            snapshot=self.snapshot,
            stage_type=SurveyAIAnalysisStage.StageType.STATISTICS,
            status=SurveyAIAnalysisStage.Status.FAILED,
            input_hash="f" * 64,
            schema_version="1",
            prompt_version="1",
            model_name="gemini-2.5-flash",
            revision=1,
            error_code="schema_invalid",
            token_metrics={
                "attempts": [
                    {
                        "validation_reason": "provider_schema_definition",
                        "http_status": 400,
                    }
                ]
            },
        )
        payload = self.client.get(self.status_url()).json()
        self.assertEqual(payload["report"]["report_source"], "legacy")
        self.assertEqual(payload["freshness"]["latest_ai_status"], "failed")
        self.assertEqual(
            payload["freshness"]["latest_error_message"],
            "AI 服務拒絕目前階段的輸出結構，請更新後重試。",
        )

    def test_newer_success_attempt_hides_historical_stage_error(self):
        SurveyAIAnalysisStage.objects.create(
            snapshot=self.snapshot,
            stage_type=SurveyAIAnalysisStage.StageType.STATISTICS,
            status=SurveyAIAnalysisStage.Status.FAILED,
            input_hash="f" * 64,
            schema_version="2",
            prompt_version="2",
            model_name="gemini-2.5-flash",
            revision=1,
            error_code="schema_invalid",
        )
        SurveyAIAnalysisStage.objects.create(
            snapshot=self.snapshot,
            stage_type=SurveyAIAnalysisStage.StageType.STATISTICS,
            status=SurveyAIAnalysisStage.Status.SUCCEEDED,
            input_hash="s" * 64,
            schema_version="2",
            prompt_version="2",
            model_name="gemini-2.5-flash",
            revision=2,
            output_json={},
        )

        freshness = self.client.get(self.status_url()).json()["freshness"]

        self.assertEqual(freshness["latest_ai_status"], "not_started")
        self.assertEqual(freshness["latest_error_code"], "")
        self.assertEqual(freshness["latest_error_message"], "")

    def test_stage_endpoints_require_manager_and_csrf(self):
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(self.status_url()).status_code, 403)
        self.assertEqual(
            self.client.post(self.generate_url(SurveyAIAnalysisStage.StageType.STATISTICS)).status_code,
            403,
        )
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.manager)
        self.assertEqual(
            csrf_client.post(self.generate_url(SurveyAIAnalysisStage.StageType.STATISTICS)).status_code,
            403,
        )
