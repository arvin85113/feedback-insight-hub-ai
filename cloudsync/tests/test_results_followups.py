"""Follow-ups from the C3 final review (M1–M5, M7)."""

from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from cloudsync.client import CLIENT, CloudError
from cloudsync.definitions import upsert_definition
from cloudsync.models import CloudLink, ResultUpload, SurveySyncState
from cloudsync.results import backfill_publications, build_content, record_publication, upload_results
from cloudsync.tests.test_inbox import definition
from cloudsync.tests.test_results_local import published_state
from cloudsync.tests.test_results_upload import FakeResultClient
from feedback.models import Survey, SurveyAnalysisState


class UploadFollowupTests(TestCase):
    def setUp(self):
        self.survey, _ = upsert_definition(definition(1))
        self.upload = record_publication(published_state(self.survey))

    def test_uploaded_content_is_dropped_but_failed_content_is_kept(self):  # M1
        content_hash = self.upload.content_hash
        upload_results(FakeResultClient([{"status": "applied"}]))
        self.upload.refresh_from_db()
        self.assertEqual((self.upload.status, self.upload.content, self.upload.content_hash), ("uploaded", {}, content_hash))
        other = record_publication(published_state(self.survey))
        upload_results(FakeResultClient([CloudError(CLIENT, status=400)]))
        other.refresh_from_db()
        self.assertEqual(other.status, "failed")
        self.assertNotEqual(other.content, {})

    def test_cloud_rejections_are_named(self):  # M2
        second = record_publication(published_state(self.survey))
        upload_results(FakeResultClient([CloudError(CLIENT, status=413), CloudError(CLIENT, status=400)]))
        self.assertEqual(ResultUpload.objects.get(pk=self.upload.pk).last_error, "too_large")
        self.assertEqual(ResultUpload.objects.get(pk=second.pk).last_error, "invalid")

    def test_unexpected_reply_fails_instead_of_stale(self):  # M3
        upload_results(FakeResultClient([{"unexpected": True}]))
        self.upload.refresh_from_db()
        self.assertEqual((self.upload.status, self.upload.last_error), ("failed", "bad_reply"))


class BuildContentFollowupTests(TestCase):
    def test_pipeline_versions_come_from_the_published_manifest(self):  # M5
        survey, _ = upsert_definition(definition(1))
        state = published_state(survey)
        SurveyAnalysisState.objects.filter(pk=state.pk).update(input_version=9, config_version=8)
        state.refresh_from_db()
        pipeline = build_content(state)["pipeline"]
        self.assertEqual((pipeline["input_version"], pipeline["config_version"], pipeline["pipeline_version"]), (1, 1, "p"))


class BackfillFollowupTests(TestCase):
    def test_local_only_surveys_are_not_visited(self):  # M4
        local = Survey.objects.create(title="L", slug="local")
        published_state(local)
        with patch("cloudsync.results.record_publication") as record:
            self.assertEqual(backfill_publications(), 0)
        record.assert_not_called()


class ResultsStatusQueryTests(TestCase):
    def test_query_count_does_not_grow_with_surveys(self):  # M7
        from node.status import results_status

        linked = CloudLink(api_url="https://c", node_uuid="99999999-9999-9999-9999-999999999999")
        first, _ = upsert_definition(definition(1))
        published_state(first)
        SurveySyncState.objects.get_or_create(survey=first)
        with CaptureQueriesContext(connection) as one:
            results_status(linked)
        for index in range(2, 5):
            survey, _ = upsert_definition({**definition(1), "survey_uuid": f"a{index}a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1",
                                           "slug": f"store-{index}", "questions": []})
            published_state(survey)
            SurveySyncState.objects.get_or_create(survey=survey)
        with CaptureQueriesContext(connection) as many:
            results_status(linked)
        self.assertEqual(len(many.captured_queries), len(one.captured_queries))
