from datetime import datetime, timezone as dt_timezone

from django.test import TestCase, override_settings

from cloudapi.definition import (
    add_question,
    apply_definition,
    archive_survey,
    move_question,
    serialize_definition,
    set_question_active,
    update_question,
    update_survey,
    validate_definition,
)
from cloudapi.errors import DefinitionError
from feedback.models import Answer, FeedbackSubmission, Question, Survey, SurveyCategory

QUESTION = {
    "title": "滿意度",
    "help_text": "",
    "kind": "single_choice",
    "data_type": "ordinal",
    "options_text": "好\n普通\n差",
    "is_required": True,
    "enable_keyword_tracking": False,
    "order": 1,
}


class DefinitionRoundTripTests(TestCase):
    def setUp(self):
        self.category = SurveyCategory.objects.create(name="門市")
        self.survey = Survey.objects.create(title="門市問卷", slug="store", category=self.category, definition_version=3)
        self.question = Question.objects.create(survey=self.survey, title="感想", kind="long_text", data_type="text", order=1)

    def test_serialize_contains_uuids_category_name_and_version(self):
        definition = serialize_definition(self.survey)
        self.assertEqual(definition["survey_uuid"], str(self.survey.uuid))
        self.assertEqual(definition["version"], 3)
        self.assertEqual(definition["category"], "門市")
        self.assertIsNone(definition["archived_at"])
        self.assertEqual(definition["questions"][0]["uuid"], str(self.question.uuid))
        validate_definition(definition)

    def test_apply_updates_adds_and_deactivates_but_never_deletes(self):
        submission = FeedbackSubmission.objects.create(survey=self.survey)
        Answer.objects.create(submission=submission, question=self.question, value="很好")
        definition = serialize_definition(self.survey)
        definition["questions"] = []  # the old question disappears from the definition
        add_question(definition, QUESTION)
        apply_definition(self.survey, definition, version=4)

        self.survey.refresh_from_db()
        self.assertEqual(self.survey.definition_version, 4)
        self.question.refresh_from_db()
        self.assertFalse(self.question.is_active)
        self.assertEqual(Answer.objects.count(), 1)
        added = Question.objects.get(survey=self.survey, title="滿意度")
        self.assertEqual(added.options, ["好", "普通", "差"])

    def test_category_by_name_and_clearing(self):
        definition = serialize_definition(self.survey)
        update_survey(definition, {"category": None})
        apply_definition(self.survey, definition, version=4)
        self.survey.refresh_from_db()
        self.assertIsNone(self.survey.category)
        definition["category"] = "外送"
        apply_definition(self.survey, definition, version=5)
        self.survey.refresh_from_db()
        self.assertEqual(self.survey.category.name, "外送")

    def test_invalid_question_is_rejected(self):
        definition = serialize_definition(self.survey)
        add_question(definition, {**QUESTION, "kind": "multiple_choice", "data_type": "ordinal"})
        with self.assertRaises(DefinitionError):
            apply_definition(self.survey, definition, version=4)

    def test_rejected_definition_writes_nothing(self):
        definition = serialize_definition(self.survey)
        definition["title"] = "不該寫入"
        bad = {**QUESTION, "kind": "dropdown"}  # unknown kind
        add_question(definition, bad)
        with self.assertRaises(DefinitionError):
            apply_definition(self.survey, definition, version=4)
        self.survey.refresh_from_db()
        self.assertEqual((self.survey.title, self.survey.definition_version), ("門市問卷", 3))

    def test_strict_validation(self):
        good = serialize_definition(self.survey)
        cases = {
            "unknown kind": lambda d: d["questions"][0].update(kind="dropdown"),
            "duplicate uuid": lambda d: d["questions"].append(dict(d["questions"][0])),
            "string flag": lambda d: d.update(is_active="yes"),
            "bool order": lambda d: d["questions"][0].update(order=True),
            "long title": lambda d: d.update(title="x" * 256),
            "bad slug": lambda d: d.update(slug="有中文"),
            "bad archived": lambda d: d.update(archived_at="yesterday"),
            "bad version": lambda d: d.update(version=-1),
            "choice without options": lambda d: d["questions"][0].update(kind="single_choice", data_type="nominal",
                                                                        options_text=""),
        }
        for name, mutate in cases.items():
            broken = {**good, "questions": [dict(q) for q in good["questions"]]}
            mutate(broken)
            with self.subTest(name), self.assertRaises(DefinitionError):
                validate_definition(broken)


class EditHelperTests(TestCase):
    def setUp(self):
        survey = Survey.objects.create(title="S", slug="s")
        self.definition = serialize_definition(survey)
        add_question(self.definition, {**QUESTION, "order": 1})
        add_question(self.definition, {**QUESTION, "title": "第二題", "order": 2})
        self.first, self.second = self.definition["questions"]

    def test_update_and_toggle(self):
        update_question(self.definition, self.first["uuid"], {**QUESTION, "title": "新標題"})
        self.assertEqual(self.first["title"], "新標題")
        set_question_active(self.definition, self.first["uuid"], False)
        self.assertFalse(self.first["is_active"])

    def test_move_swaps_order_with_neighbour(self):
        move_question(self.definition, self.second["uuid"], "up")
        self.assertEqual((self.first["order"], self.second["order"]), (2, 1))
        move_question(self.definition, self.second["uuid"], "up")  # already first: no change
        self.assertEqual((self.first["order"], self.second["order"]), (2, 1))

    def test_archive(self):
        archive_survey(self.definition, datetime(2026, 10, 2, tzinfo=dt_timezone.utc))
        self.assertFalse(self.definition["is_active"])
        self.assertFalse(self.definition["analysis_enabled"])
        self.assertTrue(self.definition["archived_at"].startswith("2026-10-02"))

    def test_unknown_question_is_rejected(self):
        with self.assertRaises(DefinitionError):
            set_question_active(self.definition, "00000000-0000-0000-0000-000000000000", False)

    def test_validate_rejects_missing_keys(self):
        broken = dict(self.definition)
        broken.pop("title")
        with self.assertRaises(DefinitionError):
            validate_definition(broken)
