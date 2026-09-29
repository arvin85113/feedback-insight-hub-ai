from django.test import SimpleTestCase

from .ai_grounding import ungrounded_numbers


EVIDENCE = [
    {
        "id": "stats.q2.average",
        "kind": "descriptive_statistic",
        "label": "系統整體流暢度評分（1-10分） 平均數",
        "value": 6.04,
        "sample_size": 46,
    },
    {
        "id": "distribution.q1.a",
        "kind": "categorical_distribution",
        "label": "您的所屬單位：研發部",
        "value": 0.348,
        "sample_size": 16,
    },
]


class GroundedNumberTests(SimpleTestCase):
    def test_numbers_copied_from_cited_evidence_are_allowed(self):
        text = "平均 6.04 分（樣本 46 人，量表 1-10 分），研發部占 34.8%，約 35%。"
        self.assertEqual(ungrounded_numbers(text, EVIDENCE), [])

    def test_rounding_follows_quoted_precision(self):
        self.assertEqual(ungrounded_numbers("平均約 6 分", EVIDENCE), [])
        self.assertEqual(ungrounded_numbers("平均 6.0 分", EVIDENCE), [])

    def test_invented_numbers_and_targets_are_rejected(self):
        self.assertEqual(ungrounded_numbers("目標提升至 7.5 分", EVIDENCE), ["7.5"])
        self.assertEqual(ungrounded_numbers("差距 3 分", EVIDENCE), ["3"])

    def test_numbers_require_cited_evidence(self):
        self.assertEqual(ungrounded_numbers("平均 6.04 分", []), ["6.04"])
        self.assertEqual(ungrounded_numbers("沒有數字的敘述", []), [])

    def test_chinese_numeral_percentages_are_not_trusted(self):
        self.assertEqual(ungrounded_numbers("約百分之三十", EVIDENCE), ["百分之三十"])

    def test_thousands_separator_and_p_value_thresholds(self):
        evidence = [{"kind": "statistical_test", "metric_type": "p_value", "value": 0.0002, "sample_size": 201295}]
        self.assertEqual(ungrounded_numbers("樣本 201,295 筆，p < 0.001", evidence), [])
