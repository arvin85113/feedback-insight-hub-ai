import pandas as pd
from django.test import TestCase

from feedback.analysis_adapters import AnswerInput, answer_cell
from feedback.background_analysis import calculate_statistics, descriptors
from feedback.local_service import analyze_frame, classify_values, get_survey_pandas_stats
from feedback.models import Answer, FeedbackSubmission, Question, Survey


def make_question(survey, title, kind, order, labels=(), **extra):
    excluded = extra.pop("excluded", ())
    choices = [{"code": "", "label": label, "excluded": label in excluded} for label in labels]
    return Question.objects.create(survey=survey, title=title, kind=kind, order=order, choices=choices, **extra)


def reply(survey, values):
    """values: {question: code list | raw string | None}."""

    submission = FeedbackSubmission.objects.create(survey=survey)
    for question, value in values.items():
        if value is None:
            continue
        if question.kind in ("single_choice", "multiple_choice"):
            labels = {c["code"]: c["label"] for c in question.choices}
            Answer.objects.create(submission=submission, question=question, choice_codes=value,
                                  value=", ".join(labels[code] for code in value))
        else:
            Answer.objects.create(submission=submission, question=question, value=value)
    return submission


def survey_for_path_parity():
    survey = Survey.objects.create(title="一致性", slug="parity")
    store = make_question(survey, "門市", "single_choice", 1, ["信義", "公館", "士林"])
    score = make_question(survey, "滿意度", "scale", 2, scale_min=1, scale_max=10)
    wait = make_question(survey, "等候", "single_choice", 3, ["很快", "普通", "很久", "不適用"], ordered=True,
                         excluded=("不適用",))
    rows = [
        ("c1", "9", "c1"), ("c1", "8", "c1"), ("c1", "10", "c2"), ("c1", "9", "c1"),
        ("c2", "5", "c3"), ("c2", "4", "c3"), ("c2", "6", "c2"), ("c2", "5", "c3"),
        ("c3", "7", "c2"), ("c3", "7", "c2"), ("c3", "8", "c1"), ("c3", "6", "c4"),
    ]
    for store_code, value, wait_code in rows:
        reply(survey, {store: [store_code], score: value, wait: [wait_code]})
    reply(survey, {})                                  # a reply with no answers at all
    reply(survey, {store: ["c1"], score: "11"})        # out-of-range scale value → invalid
    return survey


def chart_for(question, cells):
    frame = pd.DataFrame({f"Q_{question.id}": cells})
    return next(chart for chart in analyze_frame([question], frame)["charts"] if chart["question"] == question)


class ClassifyValuesTests(TestCase):
    def test_counts_partition_total(self):
        series = pd.Series(["低", "高", "不適用", None, "亂碼"])
        c = classify_values(series, data_type="ordinal", kind="single_choice", levels=["低", "高"], excluded=["不適用"])
        self.assertEqual((c["valid_n"], c["excluded_n"], c["missing_n"], c["invalid_n"], c["total_n"]), (2, 1, 1, 1, 5))
        self.assertEqual(list(c["valid_mask"]), [True, True, False, False, False])


class AnalysisPathTests(TestCase):
    def test_orm_and_worker_paths_agree(self):
        survey = survey_for_path_parity()
        orm = get_survey_pandas_stats(survey)
        adapter = AnswerInput.from_survey(survey, version="t")
        worker, row_count = calculate_statistics(adapter, descriptors(adapter))
        self.assertEqual(row_count, survey.submissions.count())

        def pick(rows):
            return sorted((r["method_key"], r["iv_title"], r["dv_title"], r.get("statistic"), r.get("p_value"), r.get("valid_n"))
                          for r in rows if not r.get("skipped_reason"))

        self.assertEqual(pick(orm["inferential_analysis"]), pick(worker["inferential_analysis"]))

        def counts(charts):
            return {c["question"].title: (c["valid_n"], c["missing_n"], c["excluded_n"], c["invalid_n"]) for c in charts}

        self.assertEqual(counts(orm["charts"]), counts(worker["charts"]))
        self.assertEqual(counts(orm["charts"])["等候"], (11, 2, 1, 0))
        self.assertEqual(counts(orm["charts"])["滿意度"], (12, 1, 0, 1))
        self.assertIn("kruskal_wallis", {r["method_key"] for r in orm["inferential_analysis"]})
        coverage = worker["field_coverage"]
        wait_field = next(f.name for f in adapter.fields() if f.title == "等候")
        self.assertEqual(coverage[wait_field]["excluded_n"], 1)

    def test_answer_cell_uses_current_labels(self):
        survey = Survey.objects.create(title="S", slug="s")
        question = make_question(survey, "門市", "single_choice", 1, ["信義", "公館"])
        choices = [dict(c) for c in question.choices]
        choices[0]["label"] = "信義區"
        Question.objects.filter(pk=question.pk).update(choices=choices)
        question.refresh_from_db()
        self.assertEqual(answer_cell(question, "信義", ["c1"]), "信義區")


class MultiChoiceTests(TestCase):
    def setUp(self):
        self.survey = Survey.objects.create(title="複選", slug="multi")

    def multi(self, labels, answers):
        question = make_question(self.survey, "品項", "multiple_choice", 1, labels)
        return chart_for(question, answers)

    def test_multi_choice_selection_rate_vs_check_share(self):
        chart = self.multi(["A", "B"], [["A"]] * 5 + [["A", "B"]] * 5)
        self.assertTrue(chart["multi"])
        self.assertEqual(chart["answered_n"], 10)
        a = next(r for r in chart["counts"] if r["value"] == "A")
        self.assertEqual((a["total"], a["selection_rate"], a["check_share"]), (10, 100.0, 66.67))
        self.assertNotIn("percent", a)

    def test_comma_label_multi_choice_round_trips(self):
        chart = self.multi(["珍珠, 椰果", "紅茶"], [["珍珠, 椰果"]] * 3)
        self.assertEqual([r["value"] for r in chart["counts"]], ["珍珠, 椰果"])

    def test_only_multiple_choice_is_split(self):
        question = make_question(self.survey, "單選", "single_choice", 2, ["甲, 乙", "丙"])
        chart = chart_for(question, ["甲, 乙", "甲, 乙", "丙"])
        self.assertEqual(sorted(r["value"] for r in chart["counts"]), ["丙", "甲, 乙"])

    def test_excluded_option_listed_separately(self):
        question = make_question(self.survey, "等候", "single_choice", 3, ["快", "慢", "不適用"], ordered=True,
                                 excluded=("不適用",))
        chart = chart_for(question, ["快", "不適用", "不適用", "慢"])
        self.assertEqual(chart["excluded_counts"], [{"value": "不適用", "total": 2}])
        self.assertEqual(sorted(r["value"] for r in chart["counts"]), ["快", "慢"])


class ParquetAndSnapshotTests(TestCase):
    def test_parquet_path_excluded_n_is_zero(self):
        from feedback.analysis_adapters import TableInput
        from feedback.analysis_input import AnalysisField

        fields = [AnalysisField("rating", "ordinal", True, "評分", "scale", ("1", "2", "3", "4", "5"))]
        adapter = TableInput([{"rating": 5}, {"rating": 4}, {"rating": None}], fields, version="v", name="p")
        result, _rows = calculate_statistics(adapter, descriptors(adapter))
        self.assertEqual(result["field_coverage"]["rating"]["excluded_n"], 0)
        self.assertEqual(result["field_coverage"]["rating"]["valid_n"], 2)

    def test_snapshot_names_both_rates(self):
        from feedback.ai_snapshot_service import build_statistics_snapshot

        survey = Survey.objects.create(title="快照", slug="snap")
        question = make_question(survey, "品項", "multiple_choice", 1, ["A", "B"])
        chart = chart_for(question, [["A"]] * 5 + [["A", "B"]] * 5)
        catalog, caveats = [], []
        build_statistics_snapshot([question], {"charts": [chart], "inferential_analysis": []}, catalog, caveats)
        labels = [item["label"] for item in catalog]
        self.assertTrue(any("選取率" in label for label in labels), labels)
        self.assertTrue(any("勾選次數占比" in label for label in labels), labels)
