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

    def test_posted_data_type_is_ignored_and_derived(self):
        definition = serialize_definition(self.survey)
        add_question(definition, {**QUESTION, "kind": "multiple_choice", "data_type": "ordinal"})
        apply_definition(self.survey, definition, version=4)
        self.assertEqual(Question.objects.get(survey=self.survey, title="滿意度").data_type, "nominal")

    def test_invalid_question_is_rejected(self):
        definition = serialize_definition(self.survey)
        add_question(definition, {**QUESTION, "options_text": "好\n好"})
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


import copy  # noqa: E402
from unittest import mock  # noqa: E402

from cloudapi.definition import SCHEMA_VERSION, definition_from_fields, delete_question, upgrade_v1  # noqa: E402
from feedback.test_utils import published  # noqa: E402

V1_FIXTURE = {
    "survey_uuid": "22222222-2222-2222-2222-222222222222", "version": 3, "title": "舊版", "slug": "old",
    "description": "", "is_active": True, "analysis_enabled": True, "thank_you_email_enabled": True,
    "improvement_tracking_enabled": True, "category": None, "archived_at": None,
    "questions": [
        {"uuid": "33333333-3333-3333-3333-333333333331", "code": "q1", "title": "等候", "help_text": "",
         "kind": "single_choice", "data_type": "ordinal", "options_text": "A\nB", "is_required": True,
         "enable_keyword_tracking": False, "is_active": True, "order": 1},
        {"uuid": "33333333-3333-3333-3333-333333333332", "code": "q2", "title": "滿意度", "help_text": "",
         "kind": "scale", "data_type": "ordinal", "options_text": "1\n2\n3\n4\n5", "is_required": True,
         "enable_keyword_tracking": False, "is_active": True, "order": 2},
    ],
}


def survey_with_all_kinds():
    survey = Survey.objects.create(title="全部題型", slug="all-kinds")
    Question.objects.create(survey=survey, title="簡答", kind="short_text", order=1)
    Question.objects.create(survey=survey, title="段落", kind="long_text", enable_keyword_tracking=True, order=2)
    Question.objects.create(survey=survey, title="單選", kind="single_choice", display="dropdown", ordered=True,
                            choices=[{"code": "", "label": "低"}, {"code": "", "label": "高"},
                                     {"code": "", "label": "不適用", "excluded": True}], order=3)
    Question.objects.create(survey=survey, title="複選", kind="multiple_choice",
                            choices=[{"code": "", "label": "甲"}, {"code": "", "label": "乙"}], order=4)
    Question.objects.create(survey=survey, title="刻度", kind="scale", scale_min=0, scale_max=10,
                            scale_min_label="完全不會", scale_max_label="一定會", order=5)
    Question.objects.create(survey=survey, title="整數", kind="integer", order=6)
    Question.objects.create(survey=survey, title="小數", kind="decimal", order=7)
    return published(survey)


class DefinitionV2Tests(TestCase):
    def test_serialize_round_trip_v2(self):
        definition = serialize_definition(survey_with_all_kinds())
        self.assertEqual((definition["schema_version"], SCHEMA_VERSION), (2, 2))
        self.assertTrue(definition["published"])
        self.assertNotIn("options_text", definition["questions"][0])
        choice = definition["questions"][2]
        self.assertEqual([(c["code"], c["score"]) for c in choice["choices"]], [("c1", 1), ("c2", 2), ("c3", None)])
        self.assertEqual(validate_definition(definition), definition)

    def test_definition_from_fields_matches_serialize(self):
        survey = survey_with_all_kinds()
        questions = list(survey.questions.order_by("order", "id"))
        self.assertEqual(definition_from_fields(survey, questions, None), serialize_definition(survey))

    def test_v1_upgrade_is_deterministic(self):
        first, second = upgrade_v1(copy.deepcopy(V1_FIXTURE)), upgrade_v1(copy.deepcopy(V1_FIXTURE))
        self.assertEqual(first, second)
        self.assertEqual([c["code"] for c in first["questions"][0]["choices"]], ["c1", "c2"])
        self.assertTrue(first["questions"][0]["ordered"])
        self.assertEqual((first["questions"][1]["scale_min"], first["questions"][1]["scale_max"]), (1, 5))
        self.assertEqual((first["published"], first["published_version"]), (True, 3))

    def test_validate_returns_upgraded_copy(self):
        v1 = copy.deepcopy(V1_FIXTURE)
        upgraded = validate_definition(v1)
        self.assertEqual((v1.get("schema_version"), upgraded["schema_version"]), (None, 2))
        self.assertIn("options_text", v1["questions"][0])

    def test_rejects_data_type_not_matching_kind(self):
        definition = serialize_definition(survey_with_all_kinds())
        definition["questions"][4]["data_type"] = "continuous"
        with self.assertRaises(DefinitionError):
            validate_definition(definition)

    def test_rejects_invalid_question(self):
        definition = serialize_definition(survey_with_all_kinds())
        definition["questions"][4]["scale_max"] = 11
        with self.assertRaisesMessage(DefinitionError, "刻度終點"):
            validate_definition(definition)

    def test_missing_question_deleted_on_draft_but_deactivated_when_published(self):
        draft = Survey.objects.create(title="草稿", slug="draft")
        gone = Question.objects.create(survey=draft, title="刪掉", kind="short_text")
        definition = serialize_definition(draft)
        delete_question(definition, str(gone.uuid))
        apply_definition(draft, definition, version=1)
        self.assertFalse(Question.objects.filter(pk=gone.pk).exists())

        survey = survey_with_all_kinds()
        first = survey.questions.order_by("order").first()
        definition = serialize_definition(survey)
        delete_question(definition, str(first.uuid))
        apply_definition(survey, definition, version=survey.definition_version + 1)
        first.refresh_from_db()
        self.assertFalse(first.is_active)

    def test_apply_saves_only_changed_rows(self):
        survey = survey_with_all_kinds()
        definition = serialize_definition(survey)
        definition["is_active"] = False
        with mock.patch("feedback.signals.schedule_survey_analysis") as schedule:
            apply_definition(survey, definition, version=survey.definition_version + 1)
        schedule.assert_not_called()
        survey.refresh_from_db()
        self.assertFalse(survey.is_active)
