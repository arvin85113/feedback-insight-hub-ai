import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db.models.query import QuerySet
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .ai_report_service import (
    AIReportError,
    _provider_error,
    create_gemini_client,
    generate_report,
    validate_report_payload,
)
from .ai_snapshot_service import (
    SNAPSHOT_SCHEMA_VERSION,
    FingerprintResult,
    SourceChangedError,
    build_evidence_coverage,
    build_or_reuse_snapshot,
    calculate_data_fingerprint,
    current_prompt_version,
    format_p_value,
    serialize_evidence_for_display,
)
from .evidence_projection import project_evidence
from .local_service import _round_p_value, build_stats_payload, build_text_analysis_payload
from .models import (
    Answer,
    FeedbackSubmission,
    ImprovementUpdate,
    Question,
    Survey,
    SurveyAIReportSnapshot,
)


EMPTY_STATS = {
    "charts": [],
    "question_analysis": [],
    "inferential_analysis": [],
    "available_tests_count": 0,
    "skipped_tests_count": 0,
}
EMPTY_TEXT = {
    "keywords": [],
    "summary": {
        "total_answers": 0,
        "analyzed_answers": 0,
        "analysis_coverage": 0,
        "avg_sentiment_score": None,
    },
    "category_sentiments": [],
}


class EvidenceDisplaySemanticsTests(TestCase):
    def test_coverage_message_distinguishes_complete_and_partial_analysis(self):
        complete = build_evidence_coverage(79, 79)
        partial = build_evidence_coverage(79, 40)
        self.assertEqual(complete["message"], "AI 本次分析已涵蓋全部 79 筆聚合證據。")
        self.assertNotIn("其餘證據", complete["message"])
        self.assertEqual(
            partial["message"],
            "AI 本次分析涵蓋 40 / 79 筆聚合證據；其餘證據仍保存在完整快照中。",
        )

    def test_p_values_below_threshold_do_not_display_as_zero(self):
        self.assertEqual(format_p_value(0), "p < 0.001")
        self.assertEqual(format_p_value(0.00001), "p < 0.001")
        self.assertEqual(format_p_value(0.03749), "p = 0.037")
        self.assertEqual(format_p_value(0.1), "p = 0.1")

    def test_correlation_coefficients_use_method_specific_labels(self):
        source = {
            "statistics": {
                "statistical_tests": [
                    {
                        "test_ref": "test-1",
                        "method_key": "pearson",
                        "test_name": "Pearson 相關分析",
                        "iv_title": "等待時間",
                        "dv_title": "整體滿意度",
                    },
                    {
                        "test_ref": "test-2",
                        "method_key": "spearman",
                        "test_name": "Spearman 等級相關分析",
                        "iv_title": "服務排序",
                        "dv_title": "推薦意願",
                    },
                ]
            }
        }
        pearson = serialize_evidence_for_display(
            {"id": "test.test-1.statistic", "kind": "statistical_test", "value": 0.72},
            source,
        )
        spearman = serialize_evidence_for_display(
            {"id": "test.test-2.statistic", "kind": "statistical_test", "value": 0.61},
            source,
        )
        self.assertIn("等待時間 × 整體滿意度", pearson["display_text"])
        self.assertIn("相關係數 r：0.72", pearson["display_text"])
        self.assertIn("服務排序 × 推薦意願", spearman["display_text"])
        self.assertIn("等級相關係數 ρ：0.61", spearman["display_text"])

    def test_same_type_anova_evidence_keeps_distinct_variable_names(self):
        source = {
            "statistics": {
                "statistical_tests": [
                    {
                        "test_ref": "test-1",
                        "method_key": "one_way_anova",
                        "test_name": "單因子變異數分析 (ANOVA)",
                        "iv_title": "部門",
                        "dv_title": "系統流暢度",
                    },
                    {
                        "test_ref": "test-2",
                        "method_key": "one_way_anova",
                        "test_name": "單因子變異數分析 (ANOVA)",
                        "iv_title": "門市",
                        "dv_title": "整體滿意度",
                    },
                ]
            }
        }
        first = serialize_evidence_for_display(
            {"id": "test.test-1.statistic", "kind": "statistical_test", "value": 5.2},
            source,
        )
        second = serialize_evidence_for_display(
            {"id": "test.test-2.statistic", "kind": "statistical_test", "value": 4.1},
            source,
        )
        self.assertIn("部門 × 系統流暢度", first["display_text"])
        self.assertIn("門市 × 整體滿意度", second["display_text"])
        self.assertIn("F 統計量", first["display_text"])
        self.assertNotEqual(first["display_text"], second["display_text"])

    def test_keyword_occurrences_are_not_presented_as_sample_size(self):
        legacy = serialize_evidence_for_display(
            {
                "id": "keyword.wait",
                "kind": "keyword_frequency",
                "label": "關鍵字「等待」出現次數",
                "value": 12,
                "unit": "occurrences",
                "sample_size": 12,
            }
        )
        with_responses = serialize_evidence_for_display(
            {**legacy, "response_count": 7, "occurrence_count": 12}
        )
        self.assertEqual(legacy["display_text"], "關鍵字「等待」：共提及 12 次")
        self.assertNotIn("樣本", legacy["display_text"])
        self.assertEqual(
            with_responses["display_text"],
            "關鍵字「等待」：共提及 12 次；涵蓋 7 份文字回覆",
        )

    def test_old_cache_without_test_metadata_degrades_without_inventing_variables(self):
        evidence = serialize_evidence_for_display(
            {
                "id": "test.test-1.statistic",
                "kind": "statistical_test",
                "label": "單因子變異數分析 檢定統計量",
                "value": 3.2,
            },
            {},
        )
        self.assertEqual(evidence["display_text"], "單因子變異數分析 檢定統計量：3.2")
        self.assertNotIn(" × ", evidence["display_text"])


def source_snapshot(slug, *, evidence_value=12.4):
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "data_fingerprint": "a" * 64,
        "data_scope": {"survey_slug": slug},
        "existing_improvements": [],
        "evidence_catalog": [
            {
                "id": "stats.wait.mean",
                "kind": "descriptive_statistic",
                "label": "平均等候時間",
                "value": evidence_value,
                "unit": "minutes",
                "sample_size": 6,
            }
        ],
    }


def provider_report(slug, *, evidence_ref="stats.wait.mean", draft_title="改善尖峰等候流程"):
    finding = {
        "survey_slug": slug,
        "section": "critical_findings",
        "finding_title": "等候流程需要關注",
        "evidence_refs": [evidence_ref],
        "priority": "high",
        "rationale": "匿名聚合證據顯示此議題值得優先檢視。",
        "data_limitations": [],
    }
    return {
        "executive_summary": "目前最需要關注等候流程，並持續觀察後續趨勢。",
        "findings": [finding],
        "improvement_drafts": [
            {
                "title": draft_title,
                "summary": "檢視尖峰時段流程並由管理者確認執行內容。",
                "related_category": "等候時間",
                "priority": "high",
                "rationale": "此草稿直接對應已保存的聚合證據。",
                "evidence_refs": [evidence_ref],
                "data_limitations": [],
            }
        ],
        "data_caveats": [],
    }


class AIReportTestCase(TestCase):
    def setUp(self):
        rate_limit_override = override_settings(AI_REPORT_REQUEST_INTERVAL_SECONDS=0)
        rate_limit_override.enable()
        self.addCleanup(rate_limit_override.disable)
        user_model = get_user_model()
        self.manager = user_model.objects.create_user(
            username="manager",
            password="pass",
            role="manager",
        )
        self.customer = user_model.objects.create_user(
            username="customer",
            password="pass",
            role="customer",
        )
        self.survey = Survey.objects.create(title="服務體驗", slug="service-experience")
        self.question = Question.objects.create(
            survey=self.survey,
            title="請描述本次體驗",
            kind=Question.Kind.LONG_TEXT,
            data_type=Question.DataType.TEXT,
            enable_keyword_tracking=True,
        )

    def add_responses(self, survey=None, question=None, *, count=3, prefix="回饋"):
        survey = survey or self.survey
        question = question or self.question
        rows = []
        for index in range(count):
            submission = FeedbackSubmission.objects.create(survey=survey)
            rows.append(
                Answer.objects.create(
                    submission=submission,
                    question=question,
                    value=f"{prefix}{index}",
                    analysis_text=f"{prefix}{index}",
                    sentiment_score=0.2,
                    analysis_version="v2",
                )
            )
        return rows

    def make_snapshot(self, *, survey=None, status=SurveyAIReportSnapshot.Status.SNAPSHOT_READY, fingerprint=None):
        survey = survey or self.survey
        return SurveyAIReportSnapshot.objects.create(
            survey=survey,
            data_fingerprint=fingerprint or ("a" * 64),
            snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
            prompt_version=current_prompt_version(),
            model_name="gemini-2.5-flash",
            source_snapshot=source_snapshot(survey.slug),
            status=status,
            response_count=3,
        )


class ExistingAnalysisRegressionTests(AIReportTestCase):
    def test_text_analysis_is_get_only_and_keeps_existing_payload(self):
        self.client.force_login(self.manager)
        payload = {
            "keywords": [{"keyword": "等待", "count": 4, "category": "流程"}],
            "summary": {"total_answers": 4, "analyzed_answers": 4, "analysis_coverage": 1},
            "category_sentiments": [{"category": "流程", "positive": 0, "neutral": 1, "negative": 3, "total": 4}],
        }
        url = f"{reverse('feedback:text-analysis')}?survey={self.survey.slug}"
        with patch("feedback.views.service_client.get_text_analysis", return_value=payload):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "等待")
        self.assertContains(response, "分類情緒分布")
        self.assertNotContains(response, "AI 回饋摘要")
        self.assertEqual(self.client.post(url).status_code, 405)

    def test_existing_stats_builder_still_returns_numeric_summary(self):
        numeric = Question.objects.create(
            survey=self.survey,
            title="等候分鐘",
            kind=Question.Kind.INTEGER,
            data_type=Question.DataType.CONTINUOUS,
            order=2,
        )
        for value in (5, 10, 15):
            submission = FeedbackSubmission.objects.create(survey=self.survey)
            Answer.objects.create(submission=submission, question=numeric, value=str(value))
        payload = build_stats_payload(self.survey)
        chart = next(item for item in payload["charts"] if item["question"].pk == numeric.pk)
        self.assertEqual(chart["avg"], 10.0)

    def test_stats_page_displays_small_p_value_as_threshold(self):
        self.client.force_login(self.manager)
        payload = {
            "charts": [],
            "question_analysis": [],
            "inferential_analysis": [
                {
                    "analysis_family": "mean_comparison",
                    "method_key": "pearson",
                    "iv_title": "部門",
                    "dv_title": "滿意度",
                    "test_name": "Welch t-test",
                    "statistic": 3.21,
                    "p_value": 0.0,
                    "insight": "測試結果",
                }
            ],
        }
        url = f"{reverse('feedback:stats-overview')}?survey={self.survey.slug}"
        with patch("feedback.views.service_client.get_stats", return_value=payload):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "相關係數 r")
        self.assertContains(response, "p &lt; 0.001")

    def test_p_value_keeps_precision_before_display_formatting(self):
        self.assertEqual(_round_p_value(0.03749), 0.03749)
        self.assertEqual(_round_p_value(0.0000412), 0.000041)

    def test_keyword_payload_counts_occurrences_and_distinct_text_answers(self):
        for text in ("wait wait", "wait"):
            submission = FeedbackSubmission.objects.create(survey=self.survey)
            Answer.objects.create(
                submission=submission,
                question=self.question,
                value=text,
                analysis_text=text,
                sentiment_score=0,
                analysis_version="v2",
            )
        payload = build_text_analysis_payload(self.survey)
        keyword = next(row for row in payload["keywords"] if row["keyword"] == "wait")
        self.assertEqual(keyword["count"], 3)
        self.assertEqual(keyword["response_count"], 2)


class DashboardSurveySelectionTests(AIReportTestCase):
    def test_all_active_surveys_are_shown_including_insufficient(self):
        enough = Survey.objects.create(title="已有資料", slug="enough")
        enough_question = Question.objects.create(
            survey=enough,
            title="意見",
            kind=Question.Kind.LONG_TEXT,
            data_type=Question.DataType.TEXT,
        )
        self.add_responses(enough, enough_question, count=3)
        Survey.objects.create(title="停用問卷", slug="inactive", is_active=False)
        self.client.force_login(self.manager)
        with patch("feedback.views.service_client.get_dashboard", return_value={}):
            response = self.client.get(reverse("feedback:dashboard"))
        self.assertContains(response, "服務體驗")
        self.assertContains(response, "資料不足")
        self.assertContains(response, "已有資料")
        self.assertNotContains(response, "停用問卷")
        self.assertNotContains(response, "建立新問卷</a>")

    def test_insufficient_survey_cannot_create_snapshot(self):
        self.add_responses(count=2)
        self.client.force_login(self.manager)
        url = reverse("feedback:ai-report-snapshot", args=[self.survey.slug])
        with patch("feedback.ai_report_service.create_gemini_client") as client_factory:
            response = self.client.post(url)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "insufficient_responses")
        self.assertFalse(SurveyAIReportSnapshot.objects.exists())
        client_factory.assert_not_called()

    def test_status_does_not_offer_all_surveys_or_mix_data(self):
        self.add_responses(count=3)
        self.client.force_login(self.manager)
        response = self.client.get(reverse("feedback:ai-report-status", args=[self.survey.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["survey"]["slug"], self.survey.slug)


class FingerprintAndSnapshotTests(AIReportTestCase):
    def test_fingerprint_streams_answers_and_only_returns_final_digest(self):
        self.add_responses(count=3, prefix="不應保存的原文")
        original_iterator = QuerySet.iterator
        iterated_models = []

        def iterator_spy(queryset, *args, **kwargs):
            iterated_models.append(queryset.model)
            return original_iterator(queryset, *args, **kwargs)

        with patch.object(QuerySet, "iterator", new=iterator_spy):
            result = calculate_data_fingerprint(self.survey)
        self.assertIn(Answer, iterated_models)
        self.assertEqual(len(result.value), 64)
        self.assertFalse(SurveyAIReportSnapshot.objects.exists())

    def test_snapshot_is_private_and_isolated_by_survey(self):
        secret = "私密原始回答 test@example.com 0912345678"
        self.add_responses(count=3, prefix=secret)
        other = Survey.objects.create(title="另一份問卷", slug="other")
        other_question = Question.objects.create(
            survey=other,
            title="其他意見",
            kind=Question.Kind.LONG_TEXT,
            data_type=Question.DataType.TEXT,
        )
        self.add_responses(other, other_question, count=3, prefix="另一問卷")
        ImprovementUpdate.objects.create(survey=self.survey, title="本問卷改善", summary="處理流程")
        ImprovementUpdate.objects.create(survey=other, title="其他改善", summary="其他內容")
        ImprovementUpdate.objects.create(survey=None, title="舊資料", summary="不得混入")
        with patch("feedback.ai_snapshot_service.build_stats_payload", return_value=EMPTY_STATS), patch(
            "feedback.ai_snapshot_service.build_text_analysis_payload", return_value=EMPTY_TEXT
        ):
            first = build_or_reuse_snapshot(self.survey).snapshot
            second = build_or_reuse_snapshot(other).snapshot
        first_json = json.dumps(first.source_snapshot, ensure_ascii=False)
        second_json = json.dumps(second.source_snapshot, ensure_ascii=False)
        self.assertNotIn(secret, first_json)
        self.assertNotIn("test@example.com", first_json)
        self.assertNotIn("0912345678", first_json)
        self.assertIn("本問卷改善", first_json)
        self.assertNotIn("其他改善", first_json)
        self.assertNotIn("舊資料", first_json)
        self.assertIn("其他改善", second_json)
        self.assertNotEqual(first.data_fingerprint, second.data_fingerprint)

    def test_cache_hit_skips_full_analysis(self):
        self.add_responses(count=3)
        fingerprint = calculate_data_fingerprint(self.survey)
        snapshot = self.make_snapshot(
            status=SurveyAIReportSnapshot.Status.SUCCEEDED,
            fingerprint=fingerprint.value,
        )
        with patch("feedback.ai_snapshot_service.build_stats_payload") as stats, patch(
            "feedback.ai_snapshot_service.build_text_analysis_payload"
        ) as text:
            result = build_or_reuse_snapshot(self.survey)
        self.assertTrue(result.cache_hit)
        self.assertEqual(result.snapshot.pk, snapshot.pk)
        stats.assert_not_called()
        text.assert_not_called()

    def test_snapshot_always_has_server_generated_response_evidence(self):
        self.add_responses(count=3)
        with patch("feedback.ai_snapshot_service.build_stats_payload", return_value=EMPTY_STATS), patch(
            "feedback.ai_snapshot_service.build_text_analysis_payload", return_value=EMPTY_TEXT
        ):
            snapshot = build_or_reuse_snapshot(self.survey).snapshot
        evidence = {
            item["id"]: item
            for item in snapshot.source_snapshot["evidence_catalog"]
        }
        self.assertEqual(evidence["survey.valid_response_count"]["value"], 3)
        self.assertEqual(evidence["survey.valid_response_count"]["unit"], "responses")
        projection = snapshot.source_snapshot["evidence_projection"]
        self.assertEqual(projection["projection_version"], "1")
        self.assertEqual(projection["profiles"]["standard"]["total_evidence_count"], len(evidence))
        self.assertIn("evidence_kind_counts", projection["profiles"]["standard"])
        self.assertIn("excluded_reason_counts", projection["profiles"]["standard"])

    def test_snapshot_evidence_preserves_test_variables_and_keyword_semantics(self):
        self.add_responses(count=3)
        stats_payload = {
            "charts": [],
            "question_analysis": [],
            "inferential_analysis": [
                {
                    "analysis_family": "mean_comparison",
                    "method_key": "one_way_anova",
                    "test_name": "單因子變異數分析 (ANOVA)",
                    "iv_title": "部門",
                    "dv_title": "系統流暢度",
                    "statistic": 5.2,
                    "p_value": 0.00001,
                    "is_significant": True,
                    "groups": [],
                    "insight": "不同部門存在差異",
                }
            ],
        }
        text_payload = {
            **EMPTY_TEXT,
            "keywords": [
                {"keyword": "等待", "count": 12, "response_count": 7, "category": "流程"}
            ],
        }
        with patch("feedback.ai_snapshot_service.build_stats_payload", return_value=stats_payload), patch(
            "feedback.ai_snapshot_service.build_text_analysis_payload", return_value=text_payload
        ):
            snapshot = build_or_reuse_snapshot(self.survey).snapshot
        evidence = {row["id"]: row for row in snapshot.source_snapshot["evidence_catalog"]}
        statistic = evidence["test.test-1.statistic"]
        keyword = next(row for row in evidence.values() if row["kind"] == "keyword_frequency")
        self.assertEqual(statistic["variables"], ["部門", "系統流暢度"])
        self.assertEqual(statistic["method_key"], "one_way_anova")
        self.assertIn("部門 × 系統流暢度", statistic["label"])
        self.assertEqual(keyword["occurrence_count"], 12)
        self.assertEqual(keyword["response_count"], 7)
        self.assertIsNone(keyword["sample_size"])

    def test_source_change_twice_does_not_save_inconsistent_snapshot(self):
        self.add_responses(count=3)
        base = calculate_data_fingerprint(self.survey)
        fingerprints = [
            replace(base, value=character * 64)
            for character in ("a", "b", "c", "d")
        ]
        with patch("feedback.ai_snapshot_service.calculate_data_fingerprint", side_effect=fingerprints), patch(
            "feedback.ai_snapshot_service.build_stats_payload", return_value=EMPTY_STATS
        ), patch("feedback.ai_snapshot_service.build_text_analysis_payload", return_value=EMPTY_TEXT):
            with self.assertRaises(SourceChangedError):
                build_or_reuse_snapshot(self.survey)
        rows = SurveyAIReportSnapshot.objects.all()
        self.assertEqual(rows.count(), 2)
        self.assertTrue(all(row.status == SurveyAIReportSnapshot.Status.FAILED for row in rows))
        self.assertTrue(all(row.source_snapshot == {} for row in rows))


class StructuredReportTests(AIReportTestCase):
    def test_invalid_evidence_reference_is_rejected(self):
        payload = provider_report(self.survey.slug, evidence_ref="missing")
        with self.assertRaises(AIReportError) as raised:
            validate_report_payload(payload, source_snapshot(self.survey.slug))
        self.assertEqual(raised.exception.error_code, "schema_invalid")

    def test_evidence_values_are_resolved_from_snapshot(self):
        snapshot = source_snapshot(self.survey.slug, evidence_value=12.4)
        validated = validate_report_payload(provider_report(self.survey.slug), snapshot)
        finding = validated["critical_findings"][0]
        self.assertEqual(finding["evidence"][0]["value"], 12.4)
        self.assertEqual(finding["evidence"][0], snapshot["evidence_catalog"][0])
        self.assertEqual(validated["improvement_drafts"][0]["draft_id"], "draft-1")

    def test_ai_prose_cannot_repeat_even_supported_evidence_numbers(self):
        snapshot = source_snapshot(self.survey.slug, evidence_value=12.4)
        payload = provider_report(self.survey.slug)
        payload["executive_summary"] = "平均等候時間為 12.4 分鐘。"
        with self.assertRaises(AIReportError) as raised:
            validate_report_payload(payload, snapshot)
        self.assertEqual(raised.exception.error_code, "schema_invalid")

    def test_model_input_limits_large_evidence_catalog_without_changing_snapshot(self):
        source = source_snapshot(self.survey.slug)
        source["evidence_catalog"] = [
            {
                "id": f"keyword.{index}",
                "kind": "keyword_frequency",
                "label": f"關鍵字 {index}",
                "value": index,
                "unit": "occurrences",
                "sample_size": index,
            }
            for index in range(25)
        ]
        projected, manifest = project_evidence(source)
        self.assertEqual(len(projected["evidence_catalog"]), 25)
        self.assertEqual(manifest["selected_evidence_count"], 25)
        self.assertEqual(len(source["evidence_catalog"]), 25)

    @override_settings(GOOGLE_API_KEY="configured")
    @patch("feedback.ai_report_service.genai.Client")
    def test_express_mode_client_factory(self, client_class):
        create_gemini_client()
        client_class.assert_called_once_with(vertexai=True, api_key="configured")

    @override_settings(
        GOOGLE_API_KEY="configured",
        GEMINI_MODEL="gemini-2.5-flash",
        GEMINI_TIMEOUT_SECONDS=45,
        GEMINI_THINKING_BUDGET=512,
        GEMINI_MAX_OUTPUT_TOKENS=4096,
    )
    @patch("feedback.ai_report_service.create_gemini_client")
    def test_generate_report_uses_structured_output_without_real_api(self, client_factory):
        previous = self.make_snapshot(
            status=SurveyAIReportSnapshot.Status.SUCCEEDED,
            fingerprint="b" * 64,
        )
        snapshot = self.make_snapshot()
        client = client_factory.return_value
        client.models.generate_content.return_value = SimpleNamespace(
            text=json.dumps(provider_report(self.survey.slug), ensure_ascii=False)
        )
        result = generate_report(snapshot)
        self.assertEqual(result.status, SurveyAIReportSnapshot.Status.SUCCEEDED)
        self.assertEqual(result.ai_report["improvement_drafts"][0]["draft_id"], "draft-1")
        self.assertEqual(client.models.generate_content.call_args.kwargs["model"], snapshot.model_name)
        config = client.models.generate_content.call_args.kwargs["config"]
        self.assertEqual(config.thinking_config.thinking_budget, 512)
        self.assertEqual(config.http_options.timeout, 45000)
        self.assertEqual(config.http_options.retry_options.attempts, 1)
        self.assertEqual(config.http_options.retry_options.http_status_codes, [429])
        self.assertIsNotNone(config.response_schema)
        self.assertIsNone(config.response_json_schema)
        self.assertTrue(SurveyAIReportSnapshot.objects.filter(pk=previous.pk).exists())

    @override_settings(
        GOOGLE_API_KEY="configured",
        GEMINI_MODEL="gemini-2.5-flash",
        GEMINI_TIMEOUT_SECONDS=45,
        GEMINI_THINKING_BUDGET=512,
        GEMINI_MAX_OUTPUT_TOKENS=4096,
    )
    @patch("feedback.ai_report_service.create_gemini_client")
    def test_schema_validation_failure_retries_once_without_saving_invalid_report(self, client_factory):
        snapshot = self.make_snapshot()
        invalid = provider_report(self.survey.slug)
        invalid["executive_summary"] = "沒有證據支持提升 99%。"
        client = client_factory.return_value
        client.models.generate_content.side_effect = [
            SimpleNamespace(text=json.dumps(invalid, ensure_ascii=False)),
            SimpleNamespace(text=json.dumps(provider_report(self.survey.slug), ensure_ascii=False)),
        ]

        result = generate_report(snapshot)

        self.assertEqual(result.status, SurveyAIReportSnapshot.Status.SUCCEEDED)
        self.assertEqual(client.models.generate_content.call_count, 2)
        retry_config = client.models.generate_content.call_args_list[1].kwargs["config"]
        self.assertEqual(retry_config.thinking_config.thinking_budget, 256)
        self.assertEqual(result.ai_report["_generation"]["profile"], "compact")
        self.assertNotIn("99%", json.dumps(result.ai_report, ensure_ascii=False))

    @override_settings(
        GOOGLE_API_KEY="configured",
        GEMINI_MODEL="gemini-2.5-flash",
        GEMINI_TIMEOUT_SECONDS=45,
        GEMINI_THINKING_BUDGET=512,
        GEMINI_MAX_OUTPUT_TOKENS=4096,
    )
    @patch("feedback.ai_report_service.create_gemini_client")
    def test_stale_generating_row_can_be_recovered(self, client_factory):
        snapshot = self.make_snapshot(status=SurveyAIReportSnapshot.Status.GENERATING)
        SurveyAIReportSnapshot.objects.filter(pk=snapshot.pk).update(
            updated_at=snapshot.updated_at.replace(year=snapshot.updated_at.year - 1)
        )
        snapshot.refresh_from_db()
        client_factory.return_value.models.generate_content.return_value = SimpleNamespace(
            text=json.dumps(provider_report(self.survey.slug), ensure_ascii=False)
        )
        result = generate_report(snapshot)
        self.assertEqual(result.status, SurveyAIReportSnapshot.Status.SUCCEEDED)

    def test_provider_error_categories_are_distinct(self):
        cases = {
            401: "authentication_error",
            403: "forbidden",
            404: "model_not_found",
            429: "rate_limited",
            504: "timeout",
            400: "schema_invalid",
            500: "server_unavailable",
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                self.assertEqual(_provider_error(SimpleNamespace(code=code)).error_code, expected)
        self.assertEqual(_provider_error(TimeoutError()).error_code, "timeout")


class EndpointSecurityAndDraftTests(AIReportTestCase):
    def setUp(self):
        super().setUp()
        self.add_responses(count=3)

    def test_all_ai_endpoints_require_manager(self):
        snapshot = self.make_snapshot()
        urls = [
            ("get", reverse("feedback:ai-report-status", args=[self.survey.slug])),
            ("post", reverse("feedback:ai-report-snapshot", args=[self.survey.slug])),
            ("post", reverse("feedback:ai-report-generate", args=[self.survey.slug, snapshot.pk])),
            (
                "get",
                reverse(
                    "feedback:ai-improvement-draft",
                    args=[self.survey.slug, snapshot.pk, "draft-1"],
                ),
            ),
        ]
        self.client.force_login(self.customer)
        for method, url in urls:
            with self.subTest(url=url):
                self.assertEqual(getattr(self.client, method)(url).status_code, 403)

    def test_post_endpoints_require_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.manager)
        snapshot = self.make_snapshot()
        self.assertEqual(
            csrf_client.post(reverse("feedback:ai-report-snapshot", args=[self.survey.slug])).status_code,
            403,
        )
        self.assertEqual(
            csrf_client.post(
                reverse("feedback:ai-report-generate", args=[self.survey.slug, snapshot.pk])
            ).status_code,
            403,
        )

    def test_snapshot_cannot_be_used_with_another_survey(self):
        other = Survey.objects.create(title="其他", slug="other")
        snapshot = self.make_snapshot()
        self.client.force_login(self.manager)
        with patch("feedback.views.generate_report") as generate:
            response = self.client.post(reverse("feedback:ai-report-generate", args=[other.slug, snapshot.pk]))
        self.assertEqual(response.status_code, 404)
        generate.assert_not_called()

    def test_failed_update_keeps_previous_successful_report(self):
        previous = self.make_snapshot(status=SurveyAIReportSnapshot.Status.SUCCEEDED, fingerprint="1" * 64)
        previous.ai_report = validate_report_payload(provider_report(self.survey.slug), previous.source_snapshot)
        previous.generated_at = previous.created_at
        previous.save(update_fields=["ai_report", "generated_at"])
        current = self.make_snapshot(status=SurveyAIReportSnapshot.Status.SNAPSHOT_READY, fingerprint="2" * 64)
        self.client.force_login(self.manager)
        with patch(
            "feedback.views.generate_report",
            side_effect=AIReportError("provider_error", "AI 服務暫時無法使用。"),
        ):
            response = self.client.post(
                reverse("feedback:ai-report-generate", args=[self.survey.slug, current.pk])
            )
        self.assertEqual(response.status_code, 502)
        status = self.client.get(reverse("feedback:ai-report-status", args=[self.survey.slug])).json()
        self.assertEqual(status["report"]["snapshot_id"], previous.pk)

    def test_ai_draft_only_prefills_then_creates_with_source_survey(self):
        snapshot = self.make_snapshot(status=SurveyAIReportSnapshot.Status.SUCCEEDED)
        snapshot.ai_report = validate_report_payload(provider_report(self.survey.slug), snapshot.source_snapshot)
        snapshot.save(update_fields=["ai_report"])
        url = reverse(
            "feedback:ai-improvement-draft",
            args=[self.survey.slug, snapshot.pk, "draft-1"],
        )
        self.client.force_login(self.manager)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "改善尖峰等候流程")
        self.assertEqual(ImprovementUpdate.objects.count(), 0)
        response = self.client.post(
            url,
            {
                "title": "管理者確認後的改善",
                "summary": "已確認內容",
                "related_category": "等候時間",
            },
        )
        self.assertEqual(response.status_code, 302)
        improvement = ImprovementUpdate.objects.get()
        self.assertEqual(improvement.survey, self.survey)
        self.assertEqual(improvement.priority, ImprovementUpdate.Priority.HIGH)
