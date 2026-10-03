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

    def test_delete_without_answers_only_deactivates_and_bumps_version(self):
        self.post(action="delete-question", question_uuid=str(self.question.uuid), question_id=self.question.pk)
        self.question.refresh_from_db()
        self.assertFalse(self.question.is_active)
        self.survey.refresh_from_db()
        self.assertEqual(self.survey.definition_version, 2)

    def test_stale_form_is_rejected(self):
        self.post(action="restore-question", question_uuid=str(self.question.uuid), question_id=self.question.pk)
        response = self.client.post(
            self.url,
            {"definition_version": 1, "action": "delete-question", "question_uuid": str(self.question.uuid)},
            follow=True,
        )
        self.assertContains(response, "版本不一致，請重新載入")
        self.question.refresh_from_db()
        self.assertTrue(self.question.is_active)

    def test_semantic_edit_of_answered_question_is_refused(self):
        Answer.objects.create(submission=FeedbackSubmission.objects.create(survey=self.survey), question=self.question, value="x")
        Question.objects.filter(pk=self.question.pk).update(has_received_answer=True)
        response = self.client.post(self.url, {
            "definition_version": 1, "action": "edit-question", "question_uuid": str(self.question.uuid),
            "question_id": self.question.pk, "title": "Q", "help_text": "", "kind": "long_text", "data_type": "text",
            "options_text": "", "is_required": "on", "order": 1,
        }, follow=True)
        self.assertContains(response, "此題已有回覆，請新增題目取代並停用舊題")
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

    def test_unassigned_surveys_keep_the_old_behaviour(self):
        plain = Survey.objects.create(title="P", slug="p")
        question = Question.objects.create(survey=plain, title="Q", kind="short_text", data_type="text", order=1)
        self.client.post(reverse("feedback:survey-builder", args=["p"]), {"action": "delete-question", "question_id": question.pk})
        self.assertFalse(Question.objects.filter(pk=question.pk).exists())  # hard delete as before
        plain.refresh_from_db()
        self.assertEqual(plain.definition_version, 0)
