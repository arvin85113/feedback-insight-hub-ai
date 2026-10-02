import io
import json
import tempfile
from pathlib import Path

from django.core.management import CommandError, call_command

from .ai_eval.cases import build_case
from .ai_eval.providers import OllamaProvider, ProviderResult, ProviderUnavailable
from .ai_eval.report import render_results, summarize
from .ai_eval.runner import run_one
from .ai_eval.strategies import GROUNDED, PLACEHOLDER, format_evidence_value, render_placeholders
from .ai_snapshot_service import calculate_data_fingerprint
from .models import SurveyAIAnalysisStage
from . import test_ai_stages
from .test_ai_stages import synthesis_payload
from .tests import AIReportTestCase


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    def generate(self, *, system, schema, contents):
        self.calls.append({"system": system, "schema": schema, "contents": contents})
        return ProviderResult(payload=self.payloads.pop(0), prompt_tokens=100, output_tokens=40,
                              latency_ms=12, finish_reason="STOP")


class EvalCase(AIReportTestCase):
    # Reuse the stage fixtures without importing the TestCase (which would rerun its tests here).
    create_success_stage = test_ai_stages.AIStageServiceTests.create_success_stage
    create_upstream_stages = test_ai_stages.AIStageServiceTests.create_upstream_stages

    def setUp(self):
        super().setUp()
        self.add_responses(count=3)
        self.snapshot = self.make_snapshot(fingerprint=calculate_data_fingerprint(self.survey).value)
        self.create_upstream_stages()
        self.case = build_case(self.snapshot)


class CaseTests(EvalCase):
    def test_case_uses_the_production_synthesis_input_with_short_aliases(self):
        self.assertEqual(self.case["provider_registry"]["E001"]["value"], 12.4)
        self.assertNotIn("stats.wait.mean", json.dumps(self.case["provider_input"], ensure_ascii=False))
        self.assertEqual(self.case["aliases"], ["E001"])
        self.assertEqual(len(self.case["input_hash"]), 64)


class GroundedStrategyTests(EvalCase):
    def test_invented_target_is_dropped_and_its_number_recorded(self):
        payload = synthesis_payload()
        payload["improvement_drafts"][0]["acceptance_criteria"] = ["平均等候降至 9.9 分鐘"]
        record = run_one(FakeProvider(payload), self.case, GROUNDED, repeat=1)
        self.assertEqual(record["outcome"], "published")
        self.assertEqual((record["kept_drafts"], record["discarded_items"]), (1, 1))
        self.assertEqual(record["discarded_numbers"], ["9.9"])
        self.assertEqual((record["prompt_tokens"], record["latency_ms"]), (100, 12))

    def test_invented_number_in_the_summary_rejects_the_run(self):
        payload = synthesis_payload()
        payload["executive_summary"] = "平均等候 15 分鐘。"
        record = run_one(FakeProvider(payload), self.case, GROUNDED, repeat=1)
        self.assertEqual((record["outcome"], record["reason"], record["ungrounded_numbers"]),
                         ("rejected", "invalid_summary", ["15"]))

    def test_grounded_number_counts_toward_numeric_share(self):
        payload = synthesis_payload()
        payload["combined_findings"][0]["rationale"] = "平均等候 12.4 分鐘，值得優先處理。"
        record = run_one(FakeProvider(payload), self.case, GROUNDED, repeat=1)
        self.assertEqual(record["outcome"], "published")
        self.assertGreater(record["numeric_share"], 0)


class PlaceholderStrategyTests(EvalCase):
    def test_codes_are_rendered_from_evidence(self):
        payload = synthesis_payload()
        payload["executive_summary"] = "平均等候 {E001} 分鐘（{E001.n} 筆），應優先改善。"
        provider = FakeProvider(payload)
        record = run_one(provider, self.case, PLACEHOLDER, repeat=1)
        self.assertEqual(record["outcome"], "published")
        self.assertEqual((record["placeholders"], record["unresolved_placeholders"], record["raw_digit_tokens"]),
                         (2, 0, 0))
        self.assertIn("所有數字一律寫成引用代號", provider.calls[0]["system"])
        self.assertNotIn("只能照抄", provider.calls[0]["system"])
        self.assertNotIn("只能照抄", json.dumps(provider.calls[0]["schema"], ensure_ascii=False))

    def test_unknown_codes_and_raw_digits_are_counted(self):
        payload = synthesis_payload()
        payload["executive_summary"] = "等候 {E999} 分鐘，約 15 分鐘。"
        record = run_one(FakeProvider(payload), self.case, PLACEHOLDER, repeat=1)
        self.assertEqual((record["unresolved_placeholders"], record["raw_digit_tokens"]), (1, 1))
        self.assertEqual(record["outcome"], "rejected")  # the raw 15 is still ungrounded

    def test_render_formats_by_evidence_kind(self):
        registry = {
            "E001": {"kind": "descriptive_statistic", "value": 12.4, "sample_size": 6},
            "E002": {"kind": "categorical_distribution", "value": 0.348, "sample_size": 16},
            "E003": {"kind": "statistical_test", "metric_type": "p_value", "value": 0.00002, "sample_size": 103},
            "E004": {"kind": "statistical_test", "metric_type": "statistic", "value": -0.6235, "sample_size": 103},
        }
        rendered, stats = render_placeholders(
            {"a": "{E001}|{E002}|{E003}|{E004}|{E002.n}", "evidence_refs": ["{E001}"]}, registry, max_refs=1
        )
        self.assertEqual(rendered["a"], "12.40|34.8%|< 0.001|-0.6235|16")
        self.assertEqual(rendered["evidence_refs"], ["{E001}"])
        self.assertEqual(stats["placeholders"], 5)
        self.assertEqual(format_evidence_value({"value": 3.0}, "value"), "3")


class AutoCiteTests(EvalCase):
    def test_codes_used_in_an_item_are_added_to_its_citations(self):
        registry = {
            "E001": {"kind": "descriptive_statistic", "value": 12.4, "sample_size": 6},
            "E002": {"kind": "descriptive_statistic", "value": 55.8449, "sample_size": 9},
        }
        item = {"title": "等候 {E002} 分", "rationale": "x", "evidence_refs": ["E001"]}
        rendered, stats = render_placeholders({"combined_findings": [item]}, registry)
        self.assertEqual(rendered["combined_findings"][0]["evidence_refs"], ["E001", "E002"])
        self.assertEqual(rendered["combined_findings"][0]["title"], "等候 55.84 分")
        self.assertEqual(stats["auto_cited"], 1)

    def test_auto_cite_respects_the_citation_limit(self):
        registry = {f"E00{i}": {"kind": "descriptive_statistic", "value": float(i), "sample_size": 3} for i in range(1, 7)}
        item = {"title": "{E005} {E006}", "evidence_refs": ["E001", "E002", "E003", "E004"]}
        rendered, stats = render_placeholders({"improvement_drafts": [item]}, registry, max_refs=4)
        self.assertEqual(rendered["improvement_drafts"][0]["evidence_refs"], ["E001", "E002", "E003", "E004"])
        self.assertEqual(stats["auto_cited"], 0)

    def test_cross_cited_code_now_publishes(self):
        payload = synthesis_payload()
        payload["combined_findings"][0]["evidence_refs"] = []
        payload["combined_findings"][0]["rationale"] = "平均等候 {E001} 分鐘。"
        record = run_one(FakeProvider(payload), self.case, PLACEHOLDER, repeat=1)
        self.assertEqual(record["outcome"], "published")
        self.assertEqual(record["auto_cited"], 1)

    def test_raw_output_is_kept_for_replay(self):
        payload = synthesis_payload()
        record = run_one(FakeProvider(payload), self.case, GROUNDED, repeat=1)
        self.assertEqual(record["raw_payload"], payload)


class ReportTests(EvalCase):
    def test_summary_and_markdown(self):
        good = synthesis_payload()
        bad = synthesis_payload()
        bad["executive_summary"] = "平均等候 15 分鐘。"
        records = [
            run_one(FakeProvider(good), self.case, GROUNDED, repeat=1),
            run_one(FakeProvider(bad), self.case, GROUNDED, repeat=2),
        ]
        rows = summarize(records)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["runs"], rows[0]["published_rate"]), (2, 0.5))
        markdown = render_results(records, model="fake-model")
        self.assertIn("| 問卷 | 寫法 |", markdown)
        self.assertIn("50%", markdown)


class CommandTests(EvalCase):
    def test_dry_run_builds_cases_without_calling_a_model(self):
        with tempfile.TemporaryDirectory() as directory:
            out = io.StringIO()
            call_command("run_ai_eval", surveys=self.survey.slug, dry_run=True, output_dir=directory, stdout=out)
            self.assertIn(self.survey.slug, out.getvalue())
            self.assertTrue((Path(directory) / "cases" / f"{self.survey.slug}.json").is_file())

    def test_replay_rescores_saved_outputs_without_a_model(self):
        with tempfile.TemporaryDirectory() as directory:
            call_command("run_ai_eval", surveys=self.survey.slug, dry_run=True, output_dir=directory, stdout=io.StringIO())
            saved = run_one(FakeProvider(synthesis_payload()), self.case, PLACEHOLDER, repeat=1)
            results = Path(directory) / "old-results.jsonl"
            results.write_text(json.dumps(saved, ensure_ascii=False) + "\n", encoding="utf-8")
            report = Path(directory) / "report.md"
            out = io.StringIO()
            call_command("run_ai_eval", surveys=self.survey.slug, replay=str(results), output_dir=directory,
                         report=str(report), stdout=out)
            self.assertIn("published", out.getvalue())
            self.assertIn("重播", report.read_text(encoding="utf-8"))

    def test_gemini_requires_explicit_paid_flag(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(CommandError):
            call_command("run_ai_eval", surveys=self.survey.slug, output_dir=directory, stdout=io.StringIO())

    def test_local_model_is_an_interface_only(self):
        with self.assertRaises(ProviderUnavailable):
            OllamaProvider("qwen3:14b").generate(system="", schema={}, contents="{}")
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(CommandError):
            call_command("run_ai_eval", surveys=self.survey.slug, provider="ollama", output_dir=directory,
                         stdout=io.StringIO())
