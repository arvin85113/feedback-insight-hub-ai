import json
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from .ai_report_service import (
    AIReportError,
    _wait_for_request_slot,
    response_schema_for_profile,
    validate_report_payload,
)
from .ai_snapshot_service import calculate_data_fingerprint, get_report_status
from .evidence_projection import STANDARD_PROFILE, project_evidence
from .models import SurveyAIReportSnapshot
from .tests import AIReportTestCase, provider_report, source_snapshot


def evidence_source(count, *, kind="keyword_frequency", label="聚合證據"):
    source = {
        "schema_version": "1",
        "data_scope": {"survey_slug": "resilience-survey", "survey_title": "韌性問卷"},
        "dashboard_metrics": {"response_count": 500, "valid_response_count": 500},
        "response_trend": [],
        "statistics": {"statistical_tests": []},
        "existing_improvements": [],
        "data_caveats": ["部分小樣本群組已隱藏。"],
        "evidence_catalog": [],
    }
    source["evidence_catalog"] = [
        {
            "id": f"evidence.{index:04d}",
            "kind": kind,
            "label": label,
            "value": count - index,
            "unit": "occurrences",
            "sample_size": count,
        }
        for index in range(count)
    ]
    return source


class EvidenceProjectionBoundaryTests(SimpleTestCase):
    @override_settings(AI_REPORT_REQUEST_INTERVAL_SECONDS=6)
    @patch("feedback.ai_report_service.time.sleep")
    @patch("feedback.ai_report_service.time.monotonic", side_effect=[95.0, 100.0])
    def test_process_request_limiter_enforces_ten_rpm_spacing(self, monotonic, sleep):
        with patch("feedback.ai_report_service._NEXT_REQUEST_AT", 100.0):
            _wait_for_request_slot()
        sleep.assert_called_once_with(5.0)
        self.assertEqual(monotonic.call_count, 2)

    def test_evidence_item_boundaries(self):
        for count in (0, 1, 39, 40, 41, 79, 100, 500):
            with self.subTest(count=count):
                model_input, manifest = project_evidence(evidence_source(count), STANDARD_PROFILE)
                expected = min(count, 40)
                self.assertEqual(len(model_input["evidence_catalog"]), expected)
                self.assertEqual(manifest["selected_evidence_count"], expected)
                self.assertEqual(manifest["excluded_evidence_count"], count - expected)

    def test_single_very_long_chinese_label_is_bounded(self):
        source = evidence_source(1, label="等候體驗" * 10000)
        model_input, manifest = project_evidence(source, STANDARD_PROFILE)
        self.assertEqual(manifest["selected_evidence_count"], 1)
        self.assertLessEqual(len(model_input["evidence_catalog"][0]["label"]), 180)
        self.assertLess(manifest["estimated_input_tokens"], 12000)

    def test_all_evidence_in_one_kind_is_supported(self):
        _, manifest = project_evidence(evidence_source(100, kind="descriptive_statistic"), STANDARD_PROFILE)
        self.assertEqual(manifest["selected_evidence_count"], 40)
        self.assertEqual(manifest["evidence_kind_counts"]["descriptive_statistic"]["selected"], 40)

    def test_stratification_keeps_significant_stats_sentiment_signs_categories_and_improvement_match(self):
        source = evidence_source(60)
        source["statistics"]["statistical_tests"] = [{"test_ref": "test-1", "is_significant": True}]
        source["existing_improvements"] = [{"title": "改善付款流程", "related_category": "付款"}]
        required = [
            {"id": "survey.valid_response_count", "kind": "survey_coverage", "label": "有效回覆數", "value": 500},
            {"id": "test.test-1.p_value", "kind": "statistical_test", "label": "顯著檢定", "value": 0.01},
            {"id": "test.test-1.effect_size", "kind": "statistical_test", "label": "顯著效果量", "value": 0.8},
            {"id": "sentiment.service.positive", "kind": "category_sentiment", "label": "服務 positive", "value": 20},
            {"id": "sentiment.service.negative", "kind": "category_sentiment", "label": "服務 negative", "value": 15},
            {"id": "sentiment.payment.neutral", "kind": "category_sentiment", "label": "付款 neutral", "value": 12},
            {"id": "improvement.payment", "kind": "keyword_frequency", "label": "付款相關議題", "value": 1},
            {"id": "stats.wait.average", "kind": "descriptive_statistic", "label": "平均等候", "value": 10},
        ]
        source["evidence_catalog"] = required + source["evidence_catalog"]
        _, manifest = project_evidence(source, STANDARD_PROFILE)
        selected = set(manifest["selected_evidence_ids"])
        for evidence_id in {row["id"] for row in required}:
            self.assertIn(evidence_id, selected)

    def test_selection_is_deterministic(self):
        source = evidence_source(79)
        first_input, first_manifest = project_evidence(source, STANDARD_PROFILE)
        second_input, second_manifest = project_evidence(source, STANDARD_PROFILE)
        self.assertEqual(first_input, second_input)
        self.assertEqual(first_manifest, second_manifest)

    def test_high_frequency_keyword_is_not_starved_by_many_sentiment_categories(self):
        source = evidence_source(0)
        source["evidence_catalog"] = [
            {
                "id": f"sentiment.category-{index}.neutral",
                "kind": "category_sentiment",
                "label": f"分類 {index} neutral",
                "value": 100 - index,
            }
            for index in range(60)
        ]
        source["evidence_catalog"].append(
            {
                "id": "keyword.top",
                "kind": "keyword_frequency",
                "label": "最高頻關鍵字",
                "value": 999,
            }
        )
        _, manifest = project_evidence(source, STANDARD_PROFILE)
        self.assertIn("keyword.top", manifest["selected_evidence_ids"])

    @override_settings(AI_REPORT_MAX_ESTIMATED_INPUT_TOKENS=1700)
    def test_token_budget_takes_priority_over_item_count(self):
        source = evidence_source(40, label="大型聚合描述" * 80)
        _, manifest = project_evidence(source, STANDARD_PROFILE)
        self.assertLess(manifest["selected_evidence_count"], 40)
        self.assertGreater(manifest["excluded_reason_counts"].get("token_budget", 0), 0)

    def test_schema_uses_only_supported_control_fields(self):
        allowed = {
            "type",
            "enum",
            "items",
            "maxItems",
            "minItems",
            "properties",
            "required",
            "description",
            "propertyOrdering",
        }

        def assert_supported(value):
            if isinstance(value, dict):
                schema_keys = set(value) & {
                    "type", "enum", "items", "maxItems", "minItems", "properties", "required",
                    "description", "propertyOrdering", "additionalProperties",
                }
                self.assertTrue(schema_keys <= allowed)
                for nested in value.values():
                    assert_supported(nested)
            elif isinstance(value, list):
                for nested in value:
                    assert_supported(nested)

        assert_supported(response_schema_for_profile(STANDARD_PROFILE))
        self.assertNotIn("additionalProperties", json.dumps(response_schema_for_profile(STANDARD_PROFILE)))


@override_settings(
    GOOGLE_API_KEY="configured",
    GEMINI_MODEL="gemini-2.5-flash",
    GEMINI_TIMEOUT_SECONDS=45,
    GEMINI_THINKING_BUDGET=512,
    GEMINI_MAX_OUTPUT_TOKENS=4096,
    GEMINI_COMPACT_THINKING_BUDGET=256,
    GEMINI_COMPACT_MAX_OUTPUT_TOKENS=2048,
    AI_REPORT_MAX_EVIDENCE_ITEMS=40,
    AI_REPORT_MAX_ESTIMATED_INPUT_TOKENS=12000,
    AI_REPORT_COMPACT_MAX_EVIDENCE_ITEMS=24,
    AI_REPORT_COMPACT_MAX_ESTIMATED_INPUT_TOKENS=6000,
    AI_REPORT_RATE_LIMIT_BACKOFF_SECONDS=6,
)
class LegacyReportCompatibilityTests(AIReportTestCase):
    """Validation and freshness of legacy single reports that may still exist in the database."""

    def test_chinese_numeric_claim_without_evidence_is_rejected(self):
        payload = provider_report(self.survey.slug)
        payload["executive_summary"] = "滿意度達百分之九十九"
        with self.assertRaises(AIReportError) as raised:
            validate_report_payload(payload, source_snapshot(self.survey.slug))
        self.assertEqual(raised.exception.error_code, "schema_invalid")
        self.assertEqual(raised.exception.reason, "numeric_prose")

    def test_projection_version_change_makes_old_report_stale(self):
        self.add_responses(count=3)
        fingerprint = calculate_data_fingerprint(self.survey)
        snapshot = self.make_snapshot(
            status=SurveyAIReportSnapshot.Status.SUCCEEDED,
            fingerprint=fingerprint.value,
        )
        snapshot.generated_at = timezone.now()
        snapshot.ai_report = validate_report_payload(provider_report(self.survey.slug), snapshot.source_snapshot)
        snapshot.save(update_fields=["generated_at", "ai_report"])
        self.assertTrue(get_report_status(self.survey)["freshness"]["is_current"])
        with patch("feedback.ai_snapshot_service.effective_prompt_version", return_value="5-p999"):
            status = get_report_status(self.survey)
        self.assertFalse(status["freshness"]["is_current"])
        self.assertTrue(status["freshness"]["latest_analysis_incomplete"])
