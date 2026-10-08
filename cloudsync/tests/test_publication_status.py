from django.test import TestCase
from django.utils import timezone

from cloudsync.definitions import upsert_definition
from cloudsync.models import CloudLink, ResultUpload
from cloudsync.publication_status import decorate_publications, publication_issues
from cloudsync.results import backfill_publications
from cloudsync.tests.test_inbox import definition
from cloudsync.tests.test_results_local import published_state
from feedback.models import Survey
from node.status import results_status


class PublicationStatusTests(TestCase):
    def setUp(self):
        self.link = CloudLink(api_url="https://fixture.invalid", node_uuid="88888888-8888-8888-8888-888888888888")

    def test_local_only_publication_is_visible_not_a_false_upload_success(self):
        survey = Survey.objects.create(title="Local-only fixture", slug="local-only")
        published_state(survey)
        survey = Survey.objects.select_related("analysis_state").get(pk=survey.pk)
        decorate_publications([survey], self.link)
        self.assertEqual(survey.ui_upload_label, "未接上雲端發布")
        self.assertEqual(publication_issues().filter(cloud_bound=False).count(), 1)
        self.assertEqual(results_status(self.link).state, "warn")
        self.assertEqual(backfill_publications(), 0)
        self.assertFalse(ResultUpload.objects.exists())

    def test_external_source_has_an_explicit_resumption_link(self):
        survey = Survey.objects.create(title="Offline parquet fixture", slug="offline-parquet")
        survey.is_external_source = True
        decorate_publications([survey], self.link)
        self.assertEqual(survey.cloud_setup_url, f"/node/datasets/?survey={survey.pk}")

    def test_bound_survey_tracks_its_current_upload(self):
        survey, _ = upsert_definition(definition(1))
        published_state(survey)
        self.assertTrue(publication_issues().filter(survey=survey, cloud_bound=True).exists())
        self.assertEqual(backfill_publications(), 1)
        ResultUpload.objects.filter(survey=survey).update(status="failed")
        survey = Survey.objects.select_related("analysis_state").get(pk=survey.pk)
        decorate_publications([survey], self.link)
        self.assertEqual(survey.ui_upload_label, "上傳需要處理")
        self.assertEqual(results_status(self.link).state, "warn")

    def test_current_publication_not_an_older_uploaded_version_is_shown(self):
        survey, _ = upsert_definition(definition(1))
        state = published_state(survey)
        ResultUpload.objects.create(survey=survey, published_at=state.published_at - timezone.timedelta(minutes=1),
                                   publish_sequence=1, status="uploaded", content_hash="fixture", content={})
        survey = Survey.objects.select_related("analysis_state").get(pk=survey.pk)
        decorate_publications([survey], self.link)
        self.assertEqual(survey.ui_upload_label, "等待補建上傳紀錄")
        self.assertEqual(backfill_publications(), 1)
        decorate_publications([survey], self.link)
        self.assertEqual(survey.ui_upload_label, "等待自動上傳")
        current = ResultUpload.objects.get(survey=survey, published_at=state.published_at)
        for status, label in (("failed", "上傳需要處理"), ("stale", "雲端未套用此版本"),
                              ("uploaded", "雲端已接收此版本")):
            ResultUpload.objects.filter(pk=current.pk).update(status=status)
            decorate_publications([survey], self.link)
            self.assertEqual(survey.ui_upload_label, label)

    def test_disconnected_bound_result_is_preserved_for_automatic_retry(self):
        survey, _ = upsert_definition(definition(1))
        published_state(survey)
        backfill_publications()
        survey = Survey.objects.select_related("analysis_state").get(pk=survey.pk)
        decorate_publications([survey], CloudLink())
        self.assertEqual(survey.ui_upload_label, "離線，結果保留待上傳")
        self.assertEqual(ResultUpload.objects.get().status, "pending")
