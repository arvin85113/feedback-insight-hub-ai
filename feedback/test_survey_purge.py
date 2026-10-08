import uuid
from io import StringIO
from unittest import mock, skipUnless

from django.apps import apps
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from cloudapi.models import (
    ChangeClock,
    InboxCounter,
    InboxSubmission,
    NodeDevice,
    PublishedResultRecord,
    SubmissionReceipt,
    SurveyChange,
    SurveyDefinitionRevision,
)
from feedback.models import (
    AnalysisJob,
    Answer,
    DatasetImportBatch,
    ExternalDatasetVersion,
    FeedbackSubmission,
    ImportedSubmissionSource,
    ImprovementDispatch,
    ImprovementNotice,
    ImprovementStatusHistory,
    ImprovementUpdate,
    KeywordCategory,
    Question,
    Survey,
    SurveyAIAnalysisStage,
    SurveyAIReportSnapshot,
    SurveyAnalysisSource,
    SurveyAnalysisState,
)
from feedback.analysis_jobs import suppress_analysis_scheduling
from feedback.survey_purge import PurgeRefused, purge_survey
from feedback.test_utils import cloud_only

NODE_MODE = apps.is_installed("cloudsync")
FULL_SURVEY_COUNTS = (2, 1, 1)  # submissions, answers, notices created by make_full_survey

TARGET_LOOKUPS = (
    (Question, "survey_id"),
    (FeedbackSubmission, "survey_id"),
    (Answer, "question__survey_id"),
    (KeywordCategory, "survey_id"),
    (ImprovementUpdate, "survey_id"),
    (ImprovementNotice, "improvement__survey_id"),
    (DatasetImportBatch, "survey_id"),
    (SurveyAnalysisSource, "survey_id"),
    (SurveyAIReportSnapshot, "survey_id"),
    (SurveyAnalysisState, "survey_id"),
    (AnalysisJob, "survey_id"),
    (SurveyDefinitionRevision, "survey_id"),
    (SurveyChange, "survey_id"),
    (InboxSubmission, "survey_id"),
    (SubmissionReceipt, "survey_id"),
    (PublishedResultRecord, "survey_id"),
)


def _hash(char):
    return char * 64


def make_full_survey(slug="target"):
    with suppress_analysis_scheduling():
        return _make_full_survey(slug)


def _make_full_survey(slug):
    survey = Survey.objects.create(title="目標問卷", slug=slug)
    question = Question.objects.create(survey=survey, code="q1", title="感想", kind="short_text", data_type="text")
    answered = FeedbackSubmission.objects.create(survey=survey)
    FeedbackSubmission.objects.create(survey=survey)  # a reply with no answers
    Answer.objects.create(submission=answered, question=question, value="好喝")
    KeywordCategory.objects.create(survey=survey, keyword="甜", category="口味")

    improvement = ImprovementUpdate.objects.create(title="改善", summary="摘要", survey=survey)
    notice = ImprovementNotice.objects.create(
        improvement=improvement, subject="通知", body="內容",
        audience_type=ImprovementNotice.AudienceType.SURVEY_RESPONDENTS, status=ImprovementNotice.Status.SENT,
    )
    ImprovementDispatch.objects.create(improvement=improvement, notice=notice, submission=answered)
    ImprovementStatusHistory.objects.create(improvement=improvement, to_status=ImprovementUpdate.Status.PLANNED)

    batch = DatasetImportBatch.objects.create(
        survey=survey, source_name="fixture", source_version="1", input_file_sha256=_hash("a"), mapping_version="1",
    )
    ImportedSubmissionSource.objects.create(
        submission=answered, batch=batch, source_namespace="fixture", source_record_key="1",
        content_sha256=_hash("b"), source_version="1",
    )
    source = SurveyAnalysisSource.objects.create(survey=survey, kind=SurveyAnalysisSource.Kind.EXTERNAL)
    version = ExternalDatasetVersion.objects.create(
        source=source, source_ref="fixture", source_version="1", source_revision="1", cleaning_version="1",
        content_sha256=_hash("c"), mapping_key="fixture", mapping_version="1", row_count=1,
    )
    source.active_external_version = version
    source.save(update_fields=["active_external_version"])

    snapshot = SurveyAIReportSnapshot.objects.create(
        survey=survey, data_fingerprint=_hash("d"), snapshot_schema_version="1", prompt_version="1",
        model_name="fixture", status=SurveyAIReportSnapshot.Status.SNAPSHOT_READY,
    )
    SurveyAIAnalysisStage.objects.create(
        snapshot=snapshot, stage_type=SurveyAIAnalysisStage.StageType.SYNTHESIS,
        status=SurveyAIAnalysisStage.Status.SUCCEEDED, input_hash=_hash("e"), schema_version="1",
        prompt_version="1", model_name="fixture",
    )
    SurveyAnalysisState.objects.get_or_create(survey=survey)
    AnalysisJob.objects.create(survey=survey, input_version=1, config_version=1, pipeline_version="fixture")
    SurveyDefinitionRevision.objects.create(survey=survey, version=1, definition={})
    ChangeClock.objects.get_or_create(pk=1)
    SurveyChange.objects.create(seq=1, survey=survey, definition_version=1)
    return survey


def _inbox_item(node, survey, *, size, state=InboxSubmission.State.PENDING):
    uid = uuid.uuid4()
    InboxSubmission.objects.create(
        submission_uuid=uid, node=node, survey=survey, envelope={}, answers_hash=_hash("f"),
        payload_hash=_hash("f"), size_bytes=size, state=state,
    )
    SubmissionReceipt.objects.create(
        submission_uuid=uid, node=node, survey=survey, submitted_at=timezone.now(), definition_version=1,
        response_sequence=1, payload_hash=_hash("f"),
        status=SubmissionReceipt.Status.QUARANTINED if state == InboxSubmission.State.QUARANTINED
        else SubmissionReceipt.Status.RECEIVED,
    )


def make_inbox_history(survey):
    """Inbox rows left behind by a survey that was once node-owned; returns another survey with one pending item."""

    node, _token = NodeDevice.issue("office")
    _inbox_item(node, survey, size=100)
    _inbox_item(node, survey, size=50, state=InboxSubmission.State.QUARANTINED)
    PublishedResultRecord.objects.create(
        publish_uuid=uuid.uuid4(), node=node, survey=survey, publish_sequence=1, content_hash=_hash("g"),
        definition_version=1, analyzed_through_sequence=0,
    )
    other = Survey.objects.create(title="其他問卷", slug="other", owner_node=node)
    _inbox_item(node, other, size=30)
    InboxCounter.objects.update_or_create(node=node, defaults={"occupied_count": 3, "occupied_bytes": 180})
    return other


def snapshot_counts():
    models = [model for model, _lookup in TARGET_LOOKUPS] + [
        Survey, ImprovementDispatch, ImprovementStatusHistory, ImportedSubmissionSource, ExternalDatasetVersion,
        SurveyAIAnalysisStage,
    ]
    counts = {model.__name__: model.objects.count() for model in models}
    counts["counter"] = list(InboxCounter.objects.values_list("occupied_count", "occupied_bytes"))
    return counts


class PurgeSurveyTests(TestCase):
    def test_dry_run_reports_real_counts_and_writes_nothing(self):
        survey = make_full_survey()
        make_inbox_history(survey)
        before = snapshot_counts()
        counts = purge_survey(survey, dry_run=True)
        self.assertEqual(snapshot_counts(), before)
        self.assertEqual(counts["feedback.Survey"], 1)
        self.assertEqual(counts["feedback.ImprovementNotice"], 1)
        self.assertEqual(counts["cloudapi.InboxSubmission"], 2)
        self.assertEqual(purge_survey(Survey.objects.get(pk=survey.pk)), counts)

    def test_purge_leaves_nothing_of_target_and_keeps_other_survey(self):
        survey = make_full_survey()
        other = make_inbox_history(survey)
        purge_survey(survey)
        self.assertFalse(Survey.objects.filter(pk=survey.pk).exists())
        for model, lookup in TARGET_LOOKUPS:
            self.assertFalse(model.objects.filter(**{lookup: survey.pk}).exists(), model.__name__)
        for model in (ImprovementUpdate, ImprovementNotice, DatasetImportBatch, ImportedSubmissionSource,
                      SurveyAnalysisSource, ExternalDatasetVersion):
            self.assertEqual(model.objects.count(), 0, model.__name__)
        self.assertTrue(Survey.objects.filter(pk=other.pk).exists())
        self.assertEqual(InboxSubmission.objects.filter(survey=other).count(), 1)
        counter = InboxCounter.objects.get()
        self.assertEqual((counter.occupied_count, counter.occupied_bytes), (1, 30))

    def test_purge_schedules_no_analysis(self):
        survey = make_full_survey()
        AnalysisJob.objects.all().delete()
        with mock.patch("feedback.analysis_jobs.schedule_survey_analysis") as schedule:
            purge_survey(survey, dry_run=True)
            purge_survey(Survey.objects.get(pk=survey.pk))
        schedule.assert_not_called()
        self.assertEqual(AnalysisJob.objects.count(), 0)

    def test_other_surveys_dispatch_and_null_improvement_survive(self):
        survey = make_full_survey()
        unrelated = ImprovementUpdate.objects.create(title="全站", summary="摘要", survey=None)
        other = ImprovementDispatch.objects.create(improvement=unrelated, submission=survey.submissions.first())
        purge_survey(survey)
        other.refresh_from_db()
        self.assertIsNone(other.submission_id)
        self.assertTrue(ImprovementUpdate.objects.filter(pk=unrelated.pk).exists())

    @cloud_only
    def test_node_owned_survey_is_refused(self):
        survey = make_full_survey()
        survey.owner_node, _token = NodeDevice.issue("office")
        survey.save(update_fields=["owner_node"])
        with self.assertRaisesMessage(PurgeRefused, "指派給節點的問卷不能清除"):
            purge_survey(survey)

    def test_failure_rolls_back_everything(self):
        survey = make_full_survey()
        with mock.patch("feedback.survey_purge._delete_survey_row", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                purge_survey(survey)
        self.assertTrue(Survey.objects.filter(pk=survey.pk).exists())
        self.assertEqual(
            (FeedbackSubmission.objects.count(), Answer.objects.count(), ImprovementNotice.objects.count()),
            FULL_SURVEY_COUNTS,
        )


@skipUnless(NODE_MODE, "本機模式專用")
class NodePurgeTests(TestCase):
    def test_node_removes_pending_acks_of_survey_replies(self):
        from cloudsync.models import PendingAck

        survey = make_full_survey()
        mine = survey.submissions.first().idempotency_key
        PendingAck.objects.create(submission_uuid=mine, payload_hash=_hash("h"))
        foreign = PendingAck.objects.create(submission_uuid=uuid.uuid4(), payload_hash=_hash("h"))
        purge_survey(survey)
        self.assertEqual(list(PendingAck.objects.values_list("pk", flat=True)), [foreign.pk])

    def test_node_removes_gemini_grants_of_survey_jobs(self):
        from django.contrib.auth import get_user_model
        from node.models import NodeAIGrant

        survey = make_full_survey()
        job = AnalysisJob.objects.filter(survey=survey).first()
        actor = get_user_model().objects.create_user(username="owner", password="x")
        NodeAIGrant.objects.create(job=job, actor=actor, identity={}, confirmation_uuid=uuid.uuid4(),
                                   expires_at=timezone.now())
        counts = purge_survey(survey)
        self.assertFalse(NodeAIGrant.objects.exists())
        self.assertFalse(Survey.objects.filter(pk=survey.pk).exists())
        self.assertEqual(counts["node.NodeAIGrant"], 1)

    def test_node_refuses_synced_survey(self):
        from cloudsync.models import SurveySyncState

        survey = make_full_survey()
        SurveySyncState.objects.create(survey=survey)
        with self.assertRaisesMessage(PurgeRefused, "由雲端同步的問卷不能在本機清除"):
            purge_survey(survey)


class PurgeCommandTests(TestCase):
    def test_command_without_confirm_is_dry_run(self):
        make_full_survey()
        out = StringIO()
        call_command("purge_survey", "--survey", "target", stdout=out)
        self.assertIn("dry-run", out.getvalue())
        self.assertIn("feedback.Survey: 1", out.getvalue())
        self.assertTrue(Survey.objects.filter(slug="target").exists())

    def test_command_with_confirm_deletes(self):
        make_full_survey()
        call_command("purge_survey", "--survey", "target", "--confirm", stdout=StringIO())
        self.assertFalse(Survey.objects.filter(slug="target").exists())

    def test_unknown_slug_raises(self):
        with self.assertRaisesMessage(CommandError, "找不到問卷：nope"):
            call_command("purge_survey", "--survey", "nope", stdout=StringIO())


class SeedResetTests(TestCase):
    def test_seed_reset_works_with_revision_change_and_notice(self):
        from feedback.management.commands.seed_demo_beverage import SURVEY_SLUG

        call_command("seed_demo_beverage", stdout=StringIO())
        survey = Survey.objects.get(slug=SURVEY_SLUG)
        self.assertTrue(SurveyDefinitionRevision.objects.filter(survey=survey).exists())  # recorded by the seed
        ChangeClock.objects.get_or_create(pk=1)
        SurveyChange.objects.create(seq=1, survey=survey, definition_version=1)
        improvement = ImprovementUpdate.objects.create(title="改善", summary="摘要", survey=survey)
        ImprovementNotice.objects.create(
            improvement=improvement, subject="通知", body="內容",
            audience_type=ImprovementNotice.AudienceType.GLOBAL, status=ImprovementNotice.Status.SENT,
        )
        call_command("seed_demo_beverage", "--reset", "--yes", stdout=StringIO())
        rebuilt = Survey.objects.get(slug=SURVEY_SLUG)
        self.assertNotEqual(rebuilt.pk, survey.pk)
        self.assertFalse(SurveyDefinitionRevision.objects.filter(survey_id=survey.pk).exists())
