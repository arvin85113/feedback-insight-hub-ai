from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from cloudapi.definition import add_question, serialize_definition
from cloudapi.errors import VersionConflict
from cloudapi.models import NodeDevice, SurveyDefinitionRevision
from cloudapi.writes import assign_survey_to_node
from feedback import survey_lifecycle
from feedback.models import Question, Survey, SurveyCategory
from feedback.test_utils import cloud_only


def draft_with_questions():
    survey = survey_lifecycle.create_draft({"title": "飲料店", "survey_uuid": None})
    definition = serialize_definition(survey)
    add_question(definition, {"title": "門市", "kind": "single_choice",
                              "choices": [{"code": "", "label": "信義"}, {"code": "", "label": "公館"}]})
    add_question(definition, {"title": "感想", "kind": "long_text"})
    survey_lifecycle.commit(survey, definition, survey.definition_version)
    return Survey.objects.get(pk=survey.pk)


def publish(survey):
    definition = serialize_definition(survey)
    definition["published"] = True
    survey_lifecycle.commit(survey, definition, survey.definition_version)
    return Survey.objects.get(pk=survey.pk)


@cloud_only
class SurveyLifecycleTests(TestCase):
    def test_create_draft_has_random_slug_and_is_unpublished(self):
        survey = survey_lifecycle.create_draft({"title": "新問卷"})
        self.assertRegex(survey.slug, r"^[a-z0-9]{8}$")
        self.assertEqual((survey.definition_version, survey.published_version, survey.owner_node), (1, None, None))
        self.assertTrue(SurveyDefinitionRevision.objects.filter(survey=survey, version=1).exists())

    def test_plain_cloud_survey_edit_goes_through_change_definition(self):
        survey = draft_with_questions()
        self.assertEqual(survey.definition_version, 2)
        self.assertTrue(SurveyDefinitionRevision.objects.filter(survey=survey, version=2).exists())
        self.assertEqual([q.code for q in survey.questions.order_by("order")], ["q1", "q2"])

    def test_copy_keeps_codes_and_counters(self):
        original = publish(draft_with_questions())
        copy = survey_lifecycle.copy_as_draft(original)
        self.assertNotEqual((copy.pk, copy.uuid, copy.slug), (original.pk, original.uuid, original.slug))
        originals = list(original.questions.order_by("order"))
        copies = list(copy.questions.order_by("order"))
        self.assertEqual([q.code for q in copies], [q.code for q in originals])
        self.assertEqual([q.choices for q in copies], [q.choices for q in originals])
        self.assertEqual([q.next_choice_number for q in copies], [q.next_choice_number for q in originals])
        self.assertNotEqual({q.uuid for q in copies} & {q.uuid for q in originals}, {q.uuid for q in copies})
        self.assertEqual(copy.next_question_number, original.next_question_number)
        self.assertEqual((copy.definition_version, copy.published_version, copy.owner_node), (1, None, None))

    def test_copy_then_question_editable(self):
        copy = survey_lifecycle.copy_as_draft(publish(draft_with_questions()))
        definition = serialize_definition(copy)
        definition["questions"][0]["title"] = "改題目"
        survey_lifecycle.commit(copy, definition, copy.definition_version)
        self.assertTrue(copy.questions.filter(title="改題目").exists())

    def test_delete_unassigned_draft_purges(self):
        survey = draft_with_questions()
        survey_lifecycle.delete_or_archive(survey, survey.definition_version)
        self.assertFalse(Survey.objects.filter(pk=survey.pk).exists())

    def test_delete_assigned_draft_archives(self):
        node, _ = NodeDevice.issue("office")
        survey = assign_survey_to_node(draft_with_questions(), node).survey
        survey_lifecycle.delete_or_archive(survey, survey.definition_version)
        survey.refresh_from_db()
        self.assertIsNotNone(survey.archived_at)

    def test_delete_published_archives(self):
        survey = publish(draft_with_questions())
        survey_lifecycle.delete_or_archive(survey, survey.definition_version)
        survey.refresh_from_db()
        self.assertIsNotNone(survey.archived_at)
        self.assertEqual(survey.published_version, survey.definition_version - 1)

    def test_delete_rechecks_under_lock(self):
        survey = draft_with_questions()
        loaded_version = survey.definition_version
        publish(survey)
        with self.assertRaises(VersionConflict):
            survey_lifecycle.delete_or_archive(survey, loaded_version)
        self.assertIsNone(Survey.objects.get(pk=survey.pk).archived_at)

    def test_category_delete_clears_published_survey_category(self):
        manager = get_user_model().objects.create_user(username="m", password="x", role="manager")
        self.client.force_login(manager)
        category = SurveyCategory.objects.create(name="門市")
        survey = publish(draft_with_questions())
        definition = serialize_definition(survey)
        definition["category"] = "門市"
        survey_lifecycle.commit(survey, definition, survey.definition_version)
        self.client.post(reverse("feedback:category-delete", args=[category.pk]))
        survey.refresh_from_db()
        self.assertIsNone(survey.category)
        self.assertFalse(SurveyCategory.objects.filter(pk=category.pk).exists())

    def test_builder_publish_and_copy_actions(self):
        manager = get_user_model().objects.create_user(username="m2", password="x", role="manager")
        self.client.force_login(manager)
        survey = draft_with_questions()
        url = reverse("feedback:survey-builder", args=[survey.slug])
        self.client.post(url, {"action": "publish", "definition_version": survey.definition_version})
        survey.refresh_from_db()
        self.assertIsNotNone(survey.published_version)
        response = self.client.post(url, {"action": "copy", "definition_version": survey.definition_version})
        copy = Survey.objects.exclude(pk=survey.pk).get()
        self.assertRedirects(response, reverse("feedback:survey-builder", args=[copy.slug]), fetch_redirect_response=False)

    def test_builder_routes_every_survey_through_commit(self):
        manager = get_user_model().objects.create_user(username="m3", password="x", role="manager")
        self.client.force_login(manager)
        survey = draft_with_questions()
        with mock.patch("feedback.survey_lifecycle.commit", wraps=survey_lifecycle.commit) as commit:
            self.client.post(reverse("feedback:survey-builder", args=[survey.slug]), {
                "action": "update-survey", "definition_version": survey.definition_version, "title": "新名稱",
                "description": "", "is_active": "on", "analysis_enabled": "on",
            })
        commit.assert_called_once()
        self.assertEqual(Survey.objects.get(pk=survey.pk).title, "新名稱")
        self.assertEqual(Question.objects.filter(survey=survey).count(), 2)


@cloud_only
class LegacyDeleteGuardTests(TestCase):
    def test_unpublished_survey_with_replies_is_archived_not_deleted(self):
        from feedback.models import FeedbackSubmission

        survey = draft_with_questions()
        FeedbackSubmission.objects.create(survey=survey)
        self.assertEqual(survey_lifecycle.delete_or_archive(survey, survey.definition_version), "archived")
        self.assertTrue(Survey.objects.filter(pk=survey.pk).exists())
