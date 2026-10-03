from django.test import TestCase, override_settings

from cloudapi.definition import add_question, serialize_definition, update_question
from cloudapi.errors import VersionConflict
from cloudapi.models import ChangeClock, NodeDevice, SurveyChange, SurveyDefinitionRevision
from cloudapi.writes import assign_survey_to_node, change_definition, create_node_survey
from feedback.models import AnalysisJob, Answer, FeedbackSubmission, Question, Survey
from feedback.test_utils import cloud_only

QUESTION = {"title": "Q", "help_text": "", "kind": "single_choice", "data_type": "nominal", "options_text": "A\nB",
            "is_required": True, "enable_keyword_tracking": False, "order": 1}


class AssignTests(TestCase):
    def test_assign_draft_records_version_1_without_backfill(self):
        node, _token = NodeDevice.issue("office")
        survey = Survey.objects.create(title="S", slug="s")
        answered = Question.objects.create(survey=survey, title="A", kind="short_text", data_type="text", order=1)
        Answer.objects.create(submission=FeedbackSubmission.objects.create(survey=survey), question=answered, value="x")

        assign_survey_to_node(survey, node)

        survey.refresh_from_db()
        self.assertEqual((survey.owner_node, survey.definition_version), (node, 1))
        self.assertFalse(Question.objects.get(pk=answered.pk).has_received_answer)
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

    def test_unanswered_new_question_stays_editable(self):
        definition = serialize_definition(self.survey)
        add_question(definition, QUESTION)
        change_definition(self.survey.uuid, expected_version=1, definition=definition)
        definition = serialize_definition(Survey.objects.get(pk=self.survey.pk))
        update_question(definition, definition["questions"][0]["uuid"], {"options_text": "A\nB\nC"})
        change_definition(self.survey.uuid, expected_version=2, definition=definition)

    @cloud_only
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


import re  # noqa: E402,F401

from django.core.management import CommandError, call_command  # noqa: E402

from cloudapi.definition import blank_definition, update_survey  # noqa: E402
from cloudapi.errors import DefinitionError, PublishBlocked, PublishedLocked  # noqa: E402
from feedback.models import SurveyAnalysisState  # noqa: E402

TEXT_QUESTION = {"title": "感想", "kind": "short_text"}


def draft_survey(title="草稿"):
    return Survey.objects.create(title=title, slug=f"d-{Survey.objects.count() + 1}")


def with_question(survey):
    definition = serialize_definition(survey)
    add_question(definition, TEXT_QUESTION)
    change_definition(survey.uuid, expected_version=survey.definition_version, definition=definition)
    return Survey.objects.get(pk=survey.pk)


def publish(survey):
    definition = serialize_definition(survey)
    definition["published"] = True
    change_definition(survey.uuid, expected_version=survey.definition_version, definition=definition)
    return Survey.objects.get(pk=survey.pk)


class LifecycleWriteTests(TestCase):
    def test_every_version_records_revision_change_only_for_node(self):
        plain = draft_survey()
        definition = serialize_definition(plain)
        update_survey(definition, {"title": "新"})
        change_definition(plain.uuid, expected_version=plain.definition_version, definition=definition)
        self.assertTrue(SurveyDefinitionRevision.objects.filter(survey=plain, version=1).exists())
        self.assertFalse(SurveyChange.objects.filter(survey=plain).exists())

    def test_publish_sets_published_and_analysis_versions(self):
        survey = publish(with_question(draft_survey()))
        self.assertEqual(survey.published_version, survey.definition_version)
        self.assertEqual(survey.analysis_definition_version, survey.definition_version)
        self.assertIsNotNone(survey.published_at)

    def test_publish_requires_a_question(self):
        with self.assertRaisesMessage(DefinitionError, "至少需要一題才能發布"):
            publish(draft_survey())

    def test_published_question_change_locked_even_without_replies(self):
        survey = publish(with_question(draft_survey()))
        definition = serialize_definition(survey)
        definition["questions"][0]["title"] = "改題目"
        with self.assertRaises(PublishedLocked):
            change_definition(survey.uuid, expected_version=survey.definition_version, definition=definition)
        definition = serialize_definition(survey)
        definition["title"] = "改名稱"
        with self.assertRaises(PublishedLocked):
            change_definition(survey.uuid, expected_version=survey.definition_version, definition=definition)

    def test_cannot_unpublish(self):
        survey = publish(with_question(draft_survey()))
        definition = serialize_definition(survey)
        definition["published"] = False
        with self.assertRaises(PublishedLocked):
            change_definition(survey.uuid, expected_version=survey.definition_version, definition=definition)

    def test_whitelist_change_keeps_published_version(self):
        survey = publish(with_question(draft_survey()))
        published_version = survey.published_version
        definition = serialize_definition(survey)
        definition["is_active"] = False
        change_definition(survey.uuid, expected_version=survey.definition_version, definition=definition)
        survey.refresh_from_db()
        self.assertEqual((survey.published_version, survey.definition_version, survey.analysis_definition_version),
                         (published_version, published_version + 1, published_version))

    def test_analysis_enabled_change_sets_analysis_definition_version(self):
        survey = publish(with_question(draft_survey()))
        definition = serialize_definition(survey)
        definition["analysis_enabled"] = False
        change_definition(survey.uuid, expected_version=survey.definition_version, definition=definition)
        survey.refresh_from_db()
        self.assertEqual(survey.analysis_definition_version, survey.definition_version)

    def test_whitelist_status_category_email_tracking_do_not_bump_versions(self):
        survey = publish(with_question(draft_survey()))
        state, _ = SurveyAnalysisState.objects.get_or_create(survey=survey)
        before = (state.input_version, state.config_version)
        definition = serialize_definition(survey)
        definition.update(is_active=False, category="門市", thank_you_email_enabled=False,
                          improvement_tracking_enabled=False)
        change_definition(survey.uuid, expected_version=survey.definition_version, definition=definition)
        state.refresh_from_db()
        self.assertEqual((state.input_version, state.config_version), before)

    def test_analysis_enabled_and_archive_bump_config(self):
        survey = publish(with_question(draft_survey()))
        state, _ = SurveyAnalysisState.objects.get_or_create(survey=survey)
        before = state.config_version
        definition = serialize_definition(survey)
        definition["analysis_enabled"] = False
        change_definition(survey.uuid, expected_version=survey.definition_version, definition=definition)
        state.refresh_from_db()
        self.assertGreater(state.config_version, before)

    def test_draft_edits_and_publish_schedule_nothing(self):
        from feedback.models import AnalysisJob

        survey = publish(with_question(draft_survey()))
        self.assertFalse(AnalysisJob.objects.filter(survey=survey).exists())

    def test_node_created_survey_is_draft_with_random_slug(self):
        node, _ = NodeDevice.issue("office")
        definition = blank_definition("44444444-4444-4444-4444-444444444444", title="門市")
        survey = create_node_survey(node, definition)[0].survey
        self.assertIsNone(survey.published_version)
        self.assertRegex(survey.slug, r"^[a-z0-9]{8}$")

    @override_settings(CLOUD_INBOX_ENABLED=False)
    def test_node_survey_cannot_publish_while_inbox_off(self):
        node, _ = NodeDevice.issue("office")
        survey = with_question(assign_survey_to_node(draft_survey(), node).survey)
        with self.assertRaises(PublishBlocked):
            publish(survey)

    @override_settings(CLOUD_INBOX_ENABLED=True)
    def test_node_survey_publish_sets_inbox_since(self):
        node, _ = NodeDevice.issue("office")
        survey = publish(with_question(assign_survey_to_node(draft_survey(), node).survey))
        self.assertIsNotNone(survey.inbox_since)

    @override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=True, CLOUD_INBOX_ENABLED=True)
    def test_assign_and_enable_inbox_refused_for_published(self):
        node, _ = NodeDevice.issue("office")
        survey = publish(with_question(draft_survey()))
        with self.assertRaises(PublishedLocked):
            assign_survey_to_node(survey, node)
        with self.assertRaisesMessage(CommandError, "已發布"):
            call_command("assign_survey_node", survey=survey.slug, node="office")
        with self.assertRaisesMessage(CommandError, "已改為發布時設定收件匣，請改用發布"):
            call_command("enable_survey_inbox", survey=survey.slug)
