from django.test import TestCase
from django.utils import timezone

from cloudapi.envelope import sha256_hex
from cloudsync.definitions import upsert_definition
from cloudsync.models import ResultUpload, SurveySyncState
from cloudsync.results import backfill_publications, build_content, record_publication
from cloudsync.tests.test_inbox import definition
from feedback.models import Survey, SurveyAnalysisState


def published_state(survey, *, ai_snapshot_id=None, snapshot_id=None):
    manifest = {"statistics": {"snapshot_id": snapshot_id, "input_version": 1, "config_version": 1, "pipeline_version": "p"},
                "text": {"snapshot_id": snapshot_id, "input_version": 1, "config_version": 1, "pipeline_version": "p"}}
    if ai_snapshot_id is not None:
        manifest["ai"] = {"snapshot_id": ai_snapshot_id, "input_version": 1, "config_version": 1,
                          "pipeline_version": "p", "model_name": "m"}
    # upsert_definition already created the state through the analysis-scheduling signals.
    state, _ = SurveyAnalysisState.objects.update_or_create(survey=survey, defaults={
        "published_at": timezone.now(), "publication_manifest": manifest,
        "published_display_payload": {"statistics": {"charts": []}, "text_analysis": {}},
        "published_ai_payload": {"summary": "舊的 AI"} if ai_snapshot_id is not None else {},
    })
    return state


class RecordPublicationTests(TestCase):
    def setUp(self):
        self.survey, _ = upsert_definition(definition(1))

    def test_identity_and_content_are_frozen_at_creation(self):
        state = published_state(self.survey)
        upload = record_publication(state)
        self.assertEqual((upload.status, upload.publish_sequence), ("pending", 1))
        self.assertEqual(upload.content_hash, sha256_hex(upload.content))
        SurveyAnalysisState.objects.filter(pk=state.pk).update(published_display_payload={"changed": True})
        upload.refresh_from_db()
        self.assertEqual(upload.content_hash, sha256_hex(upload.content))

    def test_old_ai_is_not_sent_with_new_statistics(self):
        state = published_state(self.survey, snapshot_id=None, ai_snapshot_id=999)  # AI from another snapshot
        content = build_content(state)
        self.assertEqual((content["stages"]["ai"]["current"], content["ai_payload"], content["ai_source"]), (False, None, {}))

    def test_sequence_continues_after_cloud(self):
        record_publication(published_state(self.survey))
        SurveySyncState.objects.filter(survey=self.survey).update(cloud_publish_sequence=7)
        state = published_state(self.survey)  # a NEW publication, not a resend of the first
        self.assertEqual(record_publication(state).publish_sequence, 8)

    def test_out_of_scope_surveys_create_nothing(self):
        local = Survey.objects.create(title="L", slug="local")
        self.assertIsNone(record_publication(published_state(local)))

    def test_backfill_creates_missing_upload_once(self):
        published_state(self.survey)
        self.assertEqual((backfill_publications(), backfill_publications()), (1, 0))
        self.assertEqual(ResultUpload.objects.count(), 1)

    def test_same_publication_retry_keeps_uuid_hash_and_sequence(self):
        state = published_state(self.survey)
        first = record_publication(state)
        repeated = record_publication(state)
        self.assertEqual((first.pk, first.publish_uuid, first.content_hash, first.publish_sequence),
                         (repeated.pk, repeated.publish_uuid, repeated.content_hash, repeated.publish_sequence))
        self.assertEqual(ResultUpload.objects.count(), 1)
        self.assertEqual(SurveySyncState.objects.get(survey=self.survey).local_publish_sequence, 1)


class PublishHookTests(TestCase):
    """A real worker publication creates the upload in the same transaction; a voided run creates none."""

    def setUp(self):
        import tempfile

        from cloudsync.inbox import intake
        from cloudsync.tests.test_inbox import Q1, U1, FakeInboxClient, envelope

        self.survey, _ = upsert_definition(definition(1))
        intake(envelope(1, U1, {Q1: "a"}), FakeInboxClient([]))
        self.output = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.output.cleanup)

    def test_worker_publication_creates_one_pending_upload(self):
        from django.core.management import call_command

        call_command("run_analysis_worker_once", worker_id="t", output=self.output.name)
        upload = ResultUpload.objects.get(survey=self.survey)
        self.assertEqual((upload.status, upload.content["analyzed_through_sequence"]), ("pending", 1))
        self.assertTrue(upload.content["stages"]["statistics"]["current"])

    def test_voided_publication_creates_no_upload(self):
        from unittest.mock import patch

        from django.core.management import call_command

        from feedback import analysis_worker
        from feedback.models import KeywordCategory

        original = analysis_worker.calculate_text

        def mutate_then_text(*args, **kwargs):
            KeywordCategory.objects.create(survey=self.survey, keyword="甜", category="口味")
            return original(*args, **kwargs)

        with patch.object(analysis_worker, "calculate_text", mutate_then_text):
            call_command("run_analysis_worker_once", worker_id="t", output=self.output.name)
        self.assertFalse(ResultUpload.objects.exists())
