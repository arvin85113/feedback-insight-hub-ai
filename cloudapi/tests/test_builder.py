from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from cloudapi.models import NodeDevice
from cloudapi.writes import assign_survey_to_node
from feedback.models import Answer, FeedbackSubmission, Question, Survey, SurveyCategory
from feedback.test_utils import cloud_only

User = get_user_model()


@cloud_only
class CloudBuilderForNodeSurveysTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(username="m", password="x", role=User.Role.MANAGER)
        self.client.force_login(self.manager)
        self.node, _ = NodeDevice.issue("office")
        self.category = SurveyCategory.objects.create(name="門市")
        survey = Survey.objects.create(title="S", slug="s", category=self.category)
        self.question = Question.objects.create(survey=survey, title="Q", kind="short_text", data_type="text", order=1)
        self.survey = assign_survey_to_node(survey, self.node).survey  # version 1
        self.url = reverse("feedback:survey-builder", args=["s"])

    def post(self, **data):
        return self.client.post(self.url, {"definition_version": self.survey.definition_version, **data})

    def test_builder_form_carries_version_and_question_uuid(self):
        page = self.client.get(self.url)
        self.assertContains(page, 'name="definition_version" value="1"')
        self.assertContains(page, f'name="question_uuid" value="{self.question.uuid}"')

    def test_delete_on_draft_removes_question_and_bumps_version(self):
        self.post(action="delete-question", question_uuid=str(self.question.uuid), question_id=self.question.pk)
        self.assertFalse(Question.objects.filter(pk=self.question.pk).exists())
        self.survey.refresh_from_db()
        self.assertEqual(self.survey.definition_version, 2)

    def test_stale_form_is_rejected(self):
        self.post(action="duplicate-question", question_uuid=str(self.question.uuid))
        response = self.client.post(
            self.url,
            {"definition_version": 1, "action": "delete-question", "question_uuid": str(self.question.uuid)},
            follow=True,
        )
        self.assertContains(response, "此問卷已在其他視窗修改")
        self.assertTrue(Question.objects.filter(pk=self.question.pk).exists())

    def test_question_edit_of_published_survey_is_refused(self):
        from feedback.test_utils import published

        published(self.survey)
        response = self.client.post(self.url, {
            "definition_version": 1, "action": "save-question", "question_uuid": str(self.question.uuid),
            "ui_type": "long_text", "title": "Q", "help_text": "", "is_required": "on",
        })
        self.assertContains(response, "問卷已發布，題目不能修改；請複製為新草稿")
        self.question.refresh_from_db()
        self.assertEqual(self.question.kind, "short_text")

    def test_archive_goes_through_a_version(self):
        self.client.post(reverse("feedback:survey-delete", args=["s"]), {"definition_version": 1})
        self.survey.refresh_from_db()
        self.assertIsNotNone(self.survey.archived_at)
        self.assertEqual(self.survey.definition_version, 2)

    def test_deleting_a_category_versions_its_node_surveys(self):
        self.client.post(reverse("feedback:category-delete", args=[self.category.pk]))
        self.survey.refresh_from_db()
        self.assertIsNone(self.survey.category)
        self.assertEqual(self.survey.definition_version, 2)
        self.assertTrue(Survey.objects.filter(pk=self.survey.pk).exists())

    def test_unassigned_draft_goes_through_the_versioned_path(self):
        plain = Survey.objects.create(title="P", slug="p")
        question = Question.objects.create(survey=plain, title="Q", kind="short_text", data_type="text", order=1)
        self.client.post(reverse("feedback:survey-builder", args=["p"]), {
            "action": "delete-question", "question_uuid": str(question.uuid), "definition_version": 0,
        })
        self.assertFalse(Question.objects.filter(pk=question.pk).exists())
        plain.refresh_from_db()
        self.assertEqual(plain.definition_version, 1)
