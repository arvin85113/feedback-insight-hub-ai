from django.test import TestCase, override_settings

from cloudapi.definition import add_question, serialize_definition, update_question
from cloudapi.errors import SemanticLockViolation, VersionConflict
from cloudapi.models import ChangeClock, NodeDevice, SurveyChange, SurveyDefinitionRevision
from cloudapi.writes import assign_survey_to_node, change_definition, create_node_survey
from feedback.models import AnalysisJob, Answer, FeedbackSubmission, Question, Survey

QUESTION = {"title": "Q", "help_text": "", "kind": "single_choice", "data_type": "nominal", "options_text": "A\nB",
            "is_required": True, "enable_keyword_tracking": False, "order": 1}


class AssignTests(TestCase):
    def test_assign_backfills_answered_questions_and_records_version_1(self):
        node, _token = NodeDevice.issue("office")
        survey = Survey.objects.create(title="S", slug="s")
        answered = Question.objects.create(survey=survey, title="A", kind="short_text", data_type="text", order=1)
        empty = Question.objects.create(survey=survey, title="B", kind="short_text", data_type="text", order=2)
        Answer.objects.create(submission=FeedbackSubmission.objects.create(survey=survey), question=answered, value="x")

        assign_survey_to_node(survey, node)

        survey.refresh_from_db()
        self.assertEqual((survey.owner_node, survey.definition_version), (node, 1))
        self.assertTrue(Question.objects.get(pk=answered.pk).has_received_answer)
        self.assertFalse(Question.objects.get(pk=empty.pk).has_received_answer)
        self.assertTrue(SurveyDefinitionRevision.objects.filter(survey=survey, version=1).exists())
        self.assertEqual(SurveyChange.objects.get().seq, ChangeClock.objects.get(pk=1).value)


class ChangeDefinitionTests(TestCase):
    def setUp(self):
        self.node, _ = NodeDevice.issue("office")
        revision, _ = create_node_survey(self.node, {
            "survey_uuid": "11111111-1111-1111-1111-111111111111", "version": 0, "title": "門市", "slug": "",
            "description": "", "is_active": True, "analysis_enabled": True, "thank_you_email_enabled": True,
            "improvement_tracking_enabled": True, "category": None, "archived_at": None, "questions": [],
        })
        self.survey = revision.survey

    def test_created_survey_belongs_to_node_with_version_1(self):
        self.assertEqual((self.survey.owner_node, self.survey.definition_version), (self.node, 1))
        self.assertTrue(self.survey.slug)

    def test_matching_version_bumps_and_records_change(self):
        definition = serialize_definition(self.survey)
        add_question(definition, QUESTION)
        revision = change_definition(self.survey.uuid, expected_version=1, definition=definition)
        self.assertEqual((revision.survey.definition_version, revision.definition["version"]), (2, 2))
        self.assertEqual(len(revision.definition["questions"]), 1)
        self.assertEqual(list(SurveyChange.objects.values_list("definition_version", flat=True)), [1, 2])

    def test_stale_version_is_rejected_without_writing(self):
        definition = serialize_definition(self.survey)
        add_question(definition, QUESTION)
        with self.assertRaises(VersionConflict) as caught:
            change_definition(self.survey.uuid, expected_version=0, definition=definition)
        self.assertEqual(caught.exception.current_version, 1)
        self.assertFalse(Question.objects.filter(survey=self.survey).exists())

    def test_semantic_fields_of_answered_question_are_locked(self):
        definition = serialize_definition(self.survey)
        add_question(definition, QUESTION)
        change_definition(self.survey.uuid, expected_version=1, definition=definition)
        Question.objects.filter(survey=self.survey).update(has_received_answer=True)
        definition = serialize_definition(Survey.objects.get(pk=self.survey.pk))
        question_uuid = definition["questions"][0]["uuid"]
        update_question(definition, question_uuid, {"options_text": "A\nB\nC"})
        with self.assertRaises(SemanticLockViolation):
            change_definition(self.survey.uuid, expected_version=2, definition=definition)
        update_question(definition, question_uuid, {"options_text": "A\nB", "title": "新標題"})
        change_definition(self.survey.uuid, expected_version=2, definition=definition)  # wording is free

    def test_unanswered_new_question_stays_editable(self):
        definition = serialize_definition(self.survey)
        add_question(definition, QUESTION)
        change_definition(self.survey.uuid, expected_version=1, definition=definition)
        definition = serialize_definition(Survey.objects.get(pk=self.survey.pk))
        update_question(definition, definition["questions"][0]["uuid"], {"options_text": "A\nB\nC"})
        change_definition(self.survey.uuid, expected_version=2, definition=definition)

    def test_cloud_does_not_schedule_analysis_for_node_surveys(self):
        definition = serialize_definition(self.survey)
        add_question(definition, QUESTION)
        change_definition(self.survey.uuid, expected_version=1, definition=definition)
        self.assertFalse(AnalysisJob.objects.filter(survey=self.survey).exists())

    def test_create_is_idempotent_by_uuid(self):
        again, created = create_node_survey(self.node, serialize_definition(self.survey))
        self.assertEqual((again.survey.pk, again.version, created), (self.survey.pk, 1, False))


class AssignCommandGateTests(TestCase):
    def test_command_refuses_when_prototype_disabled(self):
        from django.core.management import CommandError, call_command

        with override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=False), self.assertRaises(CommandError):
            call_command("assign_survey_node", survey="x", node="y")
