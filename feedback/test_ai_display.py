from django.test import SimpleTestCase

from feedback.ai_display import humanize_ai_payload, humanize_numbers
from feedback.ai_grounding import ungrounded_numbers
from feedback.ai_statistics_service import SYSTEM_INSTRUCTION as STATISTICS_INSTRUCTION
from feedback.ai_synthesis_service import SYSTEM_INSTRUCTION as SYNTHESIS_INSTRUCTION
from feedback.ai_text_service import SYSTEM_INSTRUCTION as TEXT_INSTRUCTION


class HumanizeNumbersTests(SimpleTestCase):
    def test_numbers_read_like_a_report(self):
        self.assertEqual(humanize_numbers("相關係數達 0.7723"), "相關係數達 0.77")
        self.assertEqual(humanize_numbers("平均 810.99，最大 22387.0"), "平均 810.99，最大 22,387")
        self.assertEqual(humanize_numbers("負面評論 10543 筆、共 201295 筆"), "負面評論 10,543 筆、共 201,295 筆")

    def test_small_values_years_and_existing_separators_are_kept(self):
        self.assertEqual(humanize_numbers("p 值 0.0004"), "p 值 0.0004")
        self.assertEqual(humanize_numbers("2026 年共 1500 筆"), "2026 年共 1500 筆")
        self.assertEqual(humanize_numbers("已有 12,345 筆"), "已有 12,345 筆")
        self.assertEqual(humanize_numbers("Q3 與 v2.5 版本"), "Q3 與 v2.5 版本")

    def test_formatted_text_still_passes_number_grounding(self):
        evidence = [{"value": 0.7723, "sample_size": 201295}, {"value": 10543, "sample_size": 167364}]
        text = humanize_numbers("係數 0.7723，負面 10543 筆，樣本 201295")
        self.assertEqual(ungrounded_numbers(text, evidence), [])

    def test_payload_text_fields_are_formatted_and_evidence_is_untouched(self):
        payload = {
            "executive_summary": "共 201295 筆",
            "combined_findings": [{"title": "係數 0.7723", "rationale": "負面 10543 筆",
                                   "evidence": [{"value": 0.7723, "label": "係數 0.7723"}]}],
            "improvement_drafts": [{"title": "t", "summary": "s 22387.0", "rationale": "r",
                                    "acceptance_criteria": ["追蹤 10543 筆"]}],
            "data_caveats": ["樣本 201295"],
        }
        out = humanize_ai_payload(payload)
        self.assertEqual(out["executive_summary"], "共 201,295 筆")
        self.assertEqual(out["combined_findings"][0]["title"], "係數 0.77")
        self.assertEqual(out["combined_findings"][0]["evidence"][0]["label"], "係數 0.7723")
        self.assertEqual(out["improvement_drafts"][0]["summary"], "s 22,387")
        self.assertEqual(out["improvement_drafts"][0]["acceptance_criteria"], ["追蹤 10,543 筆"])
        self.assertEqual(out["data_caveats"], ["樣本 201,295"])
        self.assertEqual(payload["executive_summary"], "共 201295 筆")  # stored payload unchanged
        self.assertIsNone(humanize_ai_payload(None))


class PromptRuleTests(SimpleTestCase):
    def test_every_stage_skips_data_description_and_unsupported_comparisons(self):
        for instruction in (STATISTICS_INSTRUCTION, TEXT_INSTRUCTION, SYNTHESIS_INSTRUCTION):
            self.assertIn("資料描述型發現", instruction)
            self.assertIn("被比較項目", instruction)
