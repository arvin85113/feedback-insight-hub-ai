from django.test import SimpleTestCase, TestCase

from feedback.models import Question, Survey
from feedback.question_schema import (
    analysis_excluded,
    analysis_levels,
    derive_data_type,
    fields_from_legacy,
    kind_display_for,
    normalize_question,
    question_errors,
    ui_type_of,
)


def choice_item(labels, *, kind="single_choice", ordered=False, score_start=1, excluded=()):
    return {
        "kind": kind, "display": "radio" if kind == "single_choice" else "", "ordered": ordered,
        "score_start": score_start, "scale_min": None, "scale_max": None, "scale_min_label": "", "scale_max_label": "",
        "choices": [{"code": "", "label": label, "excluded": label in excluded, "score": None} for label in labels],
        "next_choice_number": 1, "enable_keyword_tracking": False, "data_type": "", "options_text": "",
    }


def scale_item(low, high):
    return {**choice_item([], kind="scale"), "choices": [], "scale_min": low, "scale_max": high}


def ch(code, label, score, excluded=False):
    return {"code": code, "label": label, "excluded": excluded, "score": score}


class QuestionSchemaTests(SimpleTestCase):
    def test_derive_data_type_table(self):
        cases = {("short_text", False): "text", ("long_text", False): "text", ("single_choice", False): "nominal",
                 ("single_choice", True): "ordinal", ("multiple_choice", False): "nominal", ("scale", False): "ordinal",
                 ("integer", False): "discrete", ("decimal", False): "continuous"}
        for (kind, ordered), expected in cases.items():
            self.assertEqual(derive_data_type(kind, ordered=ordered), expected)

    def test_scores_follow_order_and_skip_excluded(self):
        item = normalize_question(choice_item(["很快", "普通", "很久", "不適用"], ordered=True, score_start=0, excluded={"不適用"}))
        self.assertEqual([c["score"] for c in item["choices"]], [0, 1, 2, None])
        self.assertEqual([c["code"] for c in item["choices"]], ["c1", "c2", "c3", "c4"])
        self.assertEqual(item["next_choice_number"], 5)
        self.assertEqual(item["data_type"], "ordinal")

    def test_choice_code_counter_never_reuses(self):
        item = normalize_question(choice_item(["A", "B"]))
        item["choices"].pop()
        item["choices"].append({"code": "", "label": "C", "excluded": False, "score": None})
        self.assertEqual(normalize_question(item)["choices"][-1]["code"], "c3")

    def test_errors(self):
        self.assertIn("choices", question_errors(choice_item(["A", "不適用"], ordered=True, excluded={"不適用"})))
        self.assertIn("choices", question_errors(choice_item(["A", "A"])))
        self.assertIn("choices", question_errors(choice_item(["A"], kind="multiple_choice", excluded={"A"})))
        self.assertIn("scale_min", question_errors(scale_item(2, 5)))
        self.assertIn("scale_max", question_errors(scale_item(1, 11)))
        self.assertEqual(question_errors(choice_item(["A", "B"], ordered=True)), {})
        self.assertEqual(question_errors(scale_item(0, 10)), {})

    def test_non_text_keyword_tracking_forced_off(self):
        self.assertFalse(normalize_question({**scale_item(1, 5), "enable_keyword_tracking": True})["enable_keyword_tracking"])

    def test_options_text_mirrors_choices_and_range(self):
        self.assertEqual(normalize_question(choice_item(["甲", "乙"]))["options_text"], "甲\n乙")
        self.assertEqual(normalize_question(scale_item(1, 3))["options_text"], "1\n2\n3")

    def test_ui_type_round_trip(self):
        for ui_type in ("short_text", "long_text", "radio", "dropdown", "checkbox", "scale"):
            kind, display = kind_display_for(ui_type, allow_decimal=False)
            self.assertEqual(ui_type_of(kind, display), ui_type)
        self.assertEqual(kind_display_for("number", allow_decimal=True), ("decimal", ""))
        self.assertEqual(kind_display_for("number", allow_decimal=False), ("integer", ""))

    def test_fields_from_legacy(self):
        self.assertEqual(fields_from_legacy("scale", "ordinal", "1\n2\n3\n4\n5")["scale_max"], 5)
        text_scale = fields_from_legacy("scale", "ordinal", "非常滿意\n滿意")
        self.assertEqual((text_scale["kind"], text_scale["ordered"], text_scale["display"]), ("single_choice", True, "radio"))
        self.assertEqual((fields_from_legacy("scale", "ordinal", "")["scale_min"], fields_from_legacy("scale", "ordinal", "")["scale_max"]), (1, 5))
        choice = fields_from_legacy("single_choice", "ordinal", "低\n高")
        self.assertEqual([(c["label"], c["score"]) for c in choice["choices"]], [("低", 1), ("高", 2)])

    def test_analysis_levels(self):
        q = Question(kind="scale", scale_min=0, scale_max=3)
        self.assertEqual(analysis_levels(q), ["0", "1", "2", "3"])
        q = Question(kind="single_choice", ordered=True,
                     choices=[ch("c1", "低", 1), ch("c2", "不適用", None, True), ch("c3", "高", 2)])
        self.assertEqual((analysis_levels(q), analysis_excluded(q)), (["低", "高"], ["不適用"]))


class QuestionModelTests(TestCase):
    def test_question_code_allocated_from_counter(self):
        survey = Survey.objects.create(title="S", slug="s")
        first = Question.objects.create(survey=survey, title="中文題名", kind="short_text")
        second = Question.objects.create(survey=survey, title="中文題名", kind="short_text")
        self.assertEqual((first.code, second.code), ("q1", "q2"))
        self.assertEqual(first.data_type, "text")

    def test_code_counter_skips_existing_codes(self):
        survey = Survey.objects.create(title="S", slug="s")
        Question.objects.create(survey=survey, code="q1", title="既有", kind="short_text")
        self.assertEqual(Question.objects.create(survey=survey, title="新", kind="short_text").code, "q2")

    def test_legacy_options_text_still_creates_choices(self):
        survey = Survey.objects.create(title="S", slug="s")
        q = Question.objects.create(survey=survey, title="門市", kind="single_choice", data_type="nominal", options_text="甲\n乙")
        self.assertEqual([c["code"] for c in q.choices], ["c1", "c2"])
        self.assertEqual(q.options, ["甲", "乙"])

    def test_draft_does_not_accept_responses(self):
        survey = Survey.objects.create(title="S", slug="s")
        self.assertFalse(survey.accepts_responses)
        survey.published_version = 1
        self.assertTrue(survey.accepts_responses)
