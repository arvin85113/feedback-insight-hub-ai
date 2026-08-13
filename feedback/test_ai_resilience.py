import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from .ai_report_service import (
    AIReportError,
    _wait_for_request_slot,
    generate_report,
    response_schema_for_profile,
    validate_report_payload,
)
from .ai_snapshot_service import calculate_data_fingerprint, get_report_status, serialize_report
from .evidence_projection import COMPACT_PROFILE, STANDARD_PROFILE, project_evidence
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


def gemini_response(payload, *, finish_reason="STOP", prompt_tokens=700, candidates_tokens=250, thoughts_tokens=80):
    return SimpleNamespace(
        text=json.dumps(payload, ensure_ascii=False),
        candidates=[SimpleNamespace(finish_reason=finish_reason)],
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt_tokens,
            candidates_token_count=candidates_tokens,
            thoughts_token_count=thoughts_tokens,
            total_token_count=prompt_tokens + candidates_tokens + thoughts_tokens,
        ),
        sdk_http_response=SimpleNamespace(status_code=200),
    )


class ProviderFailure(Exception):
    def __init__(self, code):
        super().__init__(f"provider-{code}")
        self.code = code


@override_settings(
    AI_REPORT_MAX_EVIDENCE_ITEMS=40,
    AI_REPORT_MAX_ESTIMATED_INPUT_TOKENS=12000,
    AI_REPORT_COMPACT_MAX_EVIDENCE_ITEMS=24,
    AI_REPORT_COMPACT_MAX_ESTIMATED_INPUT_TOKENS=6000,
)
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
class GenerationFallbackTests(AIReportTestCase):
    def _successful_response(self):
        return gemini_response(provider_report(self.survey.slug))

    @patch("feedback.ai_report_service.create_gemini_client")
    def test_standard_success_does_not_retry_and_records_usage(self, client_factory):
        snapshot = self.make_snapshot()
        client = client_factory.return_value
        client.models.generate_content.return_value = self._successful_response()
        result = generate_report(snapshot)
        self.assertEqual(client.models.generate_content.call_count, 1)
        self.assertEqual(result.ai_report["_generation"]["profile"], STANDARD_PROFILE)
        metrics = result.source_snapshot["generation_metrics"]
        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics[0]["prompt_token_count"], 700)
        self.assertEqual(metrics[0]["thinking_token_count"], 80)
        self.assertEqual(metrics[0]["finish_reason"], "STOP")

    @patch("feedback.ai_report_service.create_gemini_client")
    def test_timeout_retries_once_with_compact_profile(self, client_factory):
        snapshot = self.make_snapshot()
        client = client_factory.return_value
        client.models.generate_content.side_effect = [TimeoutError(), self._successful_response()]
        result = generate_report(snapshot)
        self.assertEqual(client.models.generate_content.call_count, 2)
        self.assertEqual(result.ai_report["_generation"]["profile"], COMPACT_PROFILE)
        self.assertEqual(result.source_snapshot["generation_metrics"][1]["retry_count"], 1)

    @patch("feedback.ai_report_service.time.sleep")
    @patch("feedback.ai_report_service.create_gemini_client")
    def test_rate_limit_uses_backoff_before_single_compact_retry(self, client_factory, sleep):
        snapshot = self.make_snapshot()
        client = client_factory.return_value
        client.models.generate_content.side_effect = [ProviderFailure(429), self._successful_response()]
        generate_report(snapshot)
        self.assertEqual(client.models.generate_content.call_count, 2)
        sleep.assert_called_once_with(6)

    @patch("feedback.ai_report_service.create_gemini_client")
    def test_auth_forbidden_and_model_not_found_do_not_retry(self, client_factory):
        expected = {401: "authentication_error", 403: "forbidden", 404: "model_not_found"}
        for index, (code, error_code) in enumerate(expected.items(), start=1):
            with self.subTest(code=code):
                snapshot = self.make_snapshot(fingerprint=str(index) * 64)
                client = Mock()
                client.models.generate_content.side_effect = ProviderFailure(code)
                client_factory.return_value = client
                with self.assertRaises(AIReportError) as raised:
                    generate_report(snapshot)
                self.assertEqual(raised.exception.error_code, error_code)
                self.assertEqual(client.models.generate_content.call_count, 1)

    @patch("feedback.ai_report_service.create_gemini_client")
    def test_output_truncated_enters_compact_mode(self, client_factory):
        snapshot = self.make_snapshot()
        client = client_factory.return_value
        client.models.generate_content.side_effect = [
            gemini_response(provider_report(self.survey.slug), finish_reason="MAX_TOKENS"),
            self._successful_response(),
        ]
        result = generate_report(snapshot)
        self.assertEqual(result.ai_report["_generation"]["profile"], COMPACT_PROFILE)
        self.assertEqual(result.source_snapshot["generation_metrics"][0]["finish_reason"], "MAX_TOKENS")

    @patch("feedback.ai_report_service.create_gemini_client")
    def test_compact_success_persists_profile_and_coverage(self, client_factory):
        snapshot = self.make_snapshot()
        snapshot.source_snapshot = evidence_source(79)
        snapshot.source_snapshot["data_scope"]["survey_slug"] = self.survey.slug
        snapshot.source_snapshot["evidence_catalog"][0]["id"] = "stats.wait.mean"
        snapshot.save(update_fields=["source_snapshot"])
        client = client_factory.return_value
        invalid = provider_report(self.survey.slug)
        invalid["executive_summary"] = "不合規數字 99"
        client.models.generate_content.side_effect = [gemini_response(invalid), self._successful_response()]
        result = generate_report(snapshot)
        generation = result.ai_report["_generation"]
        self.assertEqual(generation["profile"], COMPACT_PROFILE)
        self.assertEqual(generation["total_evidence_count"], 79)
        self.assertLessEqual(generation["selected_evidence_count"], 24)
        serialized = serialize_report(result, is_current=True, cache_hit=False)
        self.assertEqual(serialized["generation_profile"], COMPACT_PROFILE)
        self.assertEqual(serialized["evidence_coverage"]["total"], 79)

    @patch("feedback.ai_report_service.create_gemini_client")
    def test_compact_failure_preserves_previous_successful_report(self, client_factory):
        self.add_responses(count=3)
        current_fingerprint = calculate_data_fingerprint(self.survey).value
        previous = self.make_snapshot(status=SurveyAIReportSnapshot.Status.SUCCEEDED, fingerprint="1" * 64)
        previous.ai_report = validate_report_payload(provider_report(self.survey.slug), previous.source_snapshot)
        previous.generated_at = timezone.now()
        previous.save(update_fields=["ai_report", "generated_at"])
        current = self.make_snapshot(fingerprint=current_fingerprint)
        client = client_factory.return_value
        client.models.generate_content.side_effect = [TimeoutError(), ProviderFailure(503)]
        with self.assertRaises(AIReportError) as raised:
            generate_report(current)
        self.assertEqual(raised.exception.error_code, "server_unavailable")
        previous.refresh_from_db()
        current.refresh_from_db()
        self.assertEqual(previous.status, SurveyAIReportSnapshot.Status.SUCCEEDED)
        self.assertEqual(current.status, SurveyAIReportSnapshot.Status.FAILED)
        status = get_report_status(self.survey)
        self.assertEqual(status["report"]["snapshot_id"], previous.pk)
        self.assertEqual(status["freshness"]["latest_error_code"], "server_unavailable")
        self.assertTrue(status["freshness"]["latest_analysis_incomplete"])

    @patch("feedback.ai_report_service.create_gemini_client")
    def test_empty_response_and_unknown_provider_error_remain_distinct(self, client_factory):
        empty_snapshot = self.make_snapshot(fingerprint="3" * 64)
        client = Mock()
        client.models.generate_content.return_value = SimpleNamespace(
            text="",
            candidates=[SimpleNamespace(finish_reason="STOP")],
            usage_metadata=None,
            sdk_http_response=SimpleNamespace(status_code=200),
        )
        client_factory.return_value = client
        with self.assertRaises(AIReportError) as empty_error:
            generate_report(empty_snapshot)
        self.assertEqual(empty_error.exception.error_code, "empty_response")
        self.assertEqual(client.models.generate_content.call_count, 1)

        provider_snapshot = self.make_snapshot(fingerprint="4" * 64)
        client = Mock()
        client.models.generate_content.side_effect = RuntimeError("opaque provider failure")
        client_factory.return_value = client
        with self.assertRaises(AIReportError) as provider_error:
            generate_report(provider_snapshot)
        self.assertEqual(provider_error.exception.error_code, "provider_error")
        self.assertEqual(client.models.generate_content.call_count, 1)

    def test_chinese_numeric_claim_without_evidence_is_rejected(self):
        payload = provider_report(self.survey.slug)
        payload["executive_summary"] = "滿意度達百分之九十九"
        with self.assertRaises(AIReportError) as raised:
            validate_report_payload(payload, source_snapshot(self.survey.slug))
        self.assertEqual(raised.exception.error_code, "schema_invalid")
        self.assertEqual(raised.exception.reason, "numeric_prose")

    @patch("feedback.ai_report_service.create_gemini_client")
    def test_privacy_safe_logs_do_not_include_prompt_snapshot_answer_or_key(self, client_factory):
        snapshot = self.make_snapshot()
        source = source_snapshot(self.survey.slug)
        source["data_scope"]["survey_title"] = "PRIVATE_PROMPT_MARKER"
        source["evidence_catalog"][0]["label"] = "PRIVATE_ANSWER_MARKER"
        snapshot.source_snapshot = source
        snapshot.save(update_fields=["source_snapshot"])
        client_factory.return_value.models.generate_content.return_value = self._successful_response()
        with self.assertLogs("feedback.ai_report_service", level="INFO") as captured:
            generate_report(snapshot)
        output = "\n".join(captured.output)
        self.assertNotIn("PRIVATE_PROMPT_MARKER", output)
        self.assertNotIn("PRIVATE_ANSWER_MARKER", output)
        self.assertNotIn("configured", output)
        self.assertIn("selected_evidence_count", output)

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
