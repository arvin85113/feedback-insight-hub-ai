import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from .analysis_adapters import AnswerInput
from .local_service import submit_survey_payload
from .models import Answer, FeedbackSubmission, Question, Survey


class FeedbackSchemaV2Tests(TestCase):
    def setUp(self):
        self.survey = Survey.objects.create(title="Schema v2", slug="schema-v2")
        self.question = Question.objects.create(
            survey=self.survey,
            title="Overall",
            kind=Question.Kind.SCALE,
            data_type=Question.DataType.ORDINAL,
            options_text="1\n2\n3\n4\n5",
        )

    def test_submission_idempotency_reuses_one_complete_response(self):
        key = uuid.uuid4()
        first = submit_survey_payload(
            self.survey,
            user=None,
            respondent_name="",
            respondent_email="",
            consent_follow_up=False,
            answers={f"question_{self.question.pk}": "5"},
            idempotency_key=key,
        )
        second = submit_survey_payload(
            self.survey,
            user=None,
            respondent_name="",
            respondent_email="",
            consent_follow_up=False,
            answers={f"question_{self.question.pk}": "5"},
            idempotency_key=key,
        )

        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["submission_id"], second["submission_id"])
        self.assertEqual(FeedbackSubmission.objects.count(), 1)
        self.assertEqual(Answer.objects.count(), 1)

    def test_analysis_input_excludes_void_incomplete_and_inactive_data(self):
        included = FeedbackSubmission.objects.create(survey=self.survey)
        Answer.objects.create(submission=included, question=self.question, value="5")
        incomplete = FeedbackSubmission.objects.create(survey=self.survey, is_complete=False)
        Answer.objects.create(submission=incomplete, question=self.question, value="1")
        voided = FeedbackSubmission.objects.create(survey=self.survey, voided_at=included.ingested_at)
        Answer.objects.create(submission=voided, question=self.question, value="2")

        adapter = AnswerInput.from_survey(self.survey, version="test")
        field_name = adapter.fields()[0].name
        self.assertEqual(list(adapter.scan((field_name,))), [{field_name: "5"}])

        self.question.is_active = False
        self.question.save(update_fields=("is_active",))
        self.assertEqual(AnswerInput.from_survey(self.survey, version="test").fields(), ())

    def test_question_kind_and_data_type_must_be_compatible(self):
        invalid = Question(
            survey=self.survey,
            title="Invalid scale",
            kind=Question.Kind.SCALE,
            data_type=Question.DataType.CONTINUOUS,
        )
        with self.assertRaises(ValidationError):
            invalid.full_clean()

    def test_manager_delete_archives_instead_of_cascading_history(self):
        manager = get_user_model().objects.create_user(
            username="schema-manager",
            password="pass",
            role="manager",
        )
        submission = FeedbackSubmission.objects.create(survey=self.survey)
        Answer.objects.create(submission=submission, question=self.question, value="4")
        self.client.force_login(manager)

        response = self.client.post(reverse("feedback:survey-delete", args=[self.survey.slug]))

        self.assertRedirects(response, reverse("feedback:survey-manager"))
        self.survey.refresh_from_db()
        self.assertIsNotNone(self.survey.archived_at)
        self.assertFalse(self.survey.is_active)
        self.assertFalse(self.survey.analysis_enabled)
        self.assertTrue(FeedbackSubmission.objects.filter(pk=submission.pk).exists())
