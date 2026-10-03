"""Regression tests for the final whole-branch review of the survey builder redesign."""

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse

from cloudapi.definition import add_question, serialize_definition
from cloudapi.errors import DefinitionError
from cloudapi.models import SurveyDefinitionRevision
from cloudapi.writes import change_definition
from feedback import survey_lifecycle
from feedback.models import Survey
from feedback.question_schema import question_errors
from feedback.test_utils import cloud_only


def draft_with(*questions):
    survey = survey_lifecycle.create_draft({"title": "回歸"})
    definition = serialize_definition(survey)
    for question in questions:
        add_question(definition, question)
    change_definition(survey.uuid, expected_version=survey.definition_version, definition=definition)
    return Survey.objects.get(pk=survey.pk)


@cloud_only
class CounterInRevisionTests(TestCase):
    def test_revision_carries_the_question_counter_after_codes_are_allocated(self):
        survey = draft_with({"title": "A", "kind": "short_text"}, {"title": "B", "kind": "short_text"})
        revision = SurveyDefinitionRevision.objects.get(survey=survey, version=survey.definition_version)
        self.assertEqual(survey.next_question_number, 3)
        self.assertEqual(revision.definition["next_question_number"], 3)


@cloud_only
class ChoiceCodeValidationTests(TestCase):
    def item(self, codes):
        return {"kind": "single_choice", "display": "radio", "ordered": False, "score_start": 1,
                "choices": [{"code": code, "label": f"選項{i}"} for i, code in enumerate(codes)]}

    def test_duplicate_or_malformed_codes_are_errors(self):
        self.assertIn("choices", question_errors(self.item(["c1", "c1"])))
        self.assertIn("choices", question_errors(self.item(["x1"])))
        self.assertEqual(question_errors(self.item(["c1", ""])), {})

    def test_existing_question_cannot_receive_a_deleted_code(self):
        survey = draft_with({"title": "門市", "kind": "single_choice",
                             "choices": [{"code": "", "label": "甲"}, {"code": "", "label": "乙"},
                                         {"code": "", "label": "丙"}]})
        definition = serialize_definition(survey)
        definition["questions"][0]["choices"].pop()          # delete c3
        change_definition(survey.uuid, expected_version=survey.definition_version, definition=definition)
        survey.refresh_from_db()
        definition = serialize_definition(survey)
        definition["questions"][0]["choices"].append({"code": "c3", "label": "丁", "excluded": False, "score": None})
        with self.assertRaises(DefinitionError):
            change_definition(survey.uuid, expected_version=survey.definition_version, definition=definition)

    def test_new_option_on_existing_question_is_accepted(self):
        from cloudapi.definition import update_question

        survey = draft_with({"title": "門市", "kind": "single_choice",
                             "choices": [{"code": "", "label": "甲"}, {"code": "", "label": "乙"}]})
        definition = serialize_definition(survey)
        question = definition["questions"][0]
        update_question(definition, question["uuid"],
                        {"choices": question["choices"] + [{"code": "", "label": "丙", "excluded": False}]})
        change_definition(survey.uuid, expected_version=survey.definition_version, definition=definition)
        self.assertEqual([c["code"] for c in survey.questions.get().choices], ["c1", "c2", "c3"])


@cloud_only
class DirectWritePathTests(TestCase):
    def test_tracking_toggle_goes_through_the_versioned_path(self):
        manager = get_user_model().objects.create_user(username="m", password="x", role="manager")
        self.client.force_login(manager)
        survey = draft_with({"title": "A", "kind": "short_text"})
        version = survey.definition_version
        self.client.post(reverse("feedback:improvement-list"), {"action": "toggle-tracking", "survey_id": survey.pk})
        survey.refresh_from_db()
        self.assertFalse(survey.improvement_tracking_enabled)
        self.assertEqual(survey.definition_version, version + 1)
        self.assertTrue(SurveyDefinitionRevision.objects.filter(survey=survey, version=version + 1).exists())

    def test_admin_cannot_edit_definitions(self):
        from feedback.admin import QuestionInline, SurveyAdmin

        request = RequestFactory().get("/")
        request.user = get_user_model().objects.create_superuser(username="root", password="x", email="r@x.tw")
        survey = draft_with({"title": "A", "kind": "short_text"})
        admin = SurveyAdmin(Survey, AdminSite())
        self.assertIn("title", admin.get_readonly_fields(request, survey))
        inline = QuestionInline(Survey, AdminSite())
        self.assertFalse(inline.has_change_permission(request, survey))
        self.assertFalse(inline.has_add_permission(request, survey))
        self.assertFalse(inline.has_delete_permission(request, survey))

    def test_importer_refuses_to_change_a_published_survey(self):
        from pathlib import Path

        from feedback.importing.mapping import load_mapping
        from feedback.importing.service import _ensure_survey_and_questions

        mapping = load_mapping(Path(__file__).resolve().parent / "import_mappings" / "tripadvisor_hotel_reviews.json")
        survey, questions = _ensure_survey_and_questions(mapping)
        questions[-1].delete()
        with self.assertRaisesMessage(ValueError, "不相容"):
            _ensure_survey_and_questions(mapping)
