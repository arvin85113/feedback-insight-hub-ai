"""PostgreSQL-only evidence for worker lease and publication concurrency."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest import SkipTest

from django.db import close_old_connections, connection
from django.test import TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from .analysis_jobs import AnalysisJobError, claim_next_job, publish_analysis_snapshot, schedule_survey_analysis
from .analysis_sources import register_external_dataset_version
from .models import AnalysisJob, Survey, SurveyAIReportSnapshot, SurveyAnalysisState


class AnalysisJobPostgreSQLConcurrencyTests(TransactionTestCase):
    """Exercise behaviour that SQLite cannot prove."""

    reset_sequences = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if connection.vendor != "postgresql":
            raise SkipTest("需要使用隔離 PostgreSQL 執行")

    def setUp(self):
        self.survey = Survey.objects.create(title="PostgreSQL queue fixture", slug="postgres-queue-fixture")

    def _claim_together(self, worker_ids):
        barrier = Barrier(len(worker_ids) + 1)

        def claim(worker_id):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                job = claim_next_job(worker_id, lease_seconds=30)
                return None if job is None else (job.pk, job.lease_token)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=len(worker_ids)) as executor:
            futures = [executor.submit(claim, worker_id) for worker_id in worker_ids]
            barrier.wait(timeout=10)
            return [future.result(timeout=15) for future in futures]

    def _make_snapshot(self, marker):
        return SurveyAIReportSnapshot.objects.create(
            survey=self.survey,
            data_fingerprint=marker * 64,
            snapshot_schema_version="postgres-fixture-v1",
            prompt_version="postgres-fixture-v1",
            model_name="fixture-model",
            source_snapshot={
                "data_scope": {"response_count": 1},
                "statistics": {"charts": []},
                "text_analysis": {"keywords": []},
                "display_payload": {
                    "schema_version": "postgres-fixture-v1",
                    "statistics": {"charts": []},
                    "text_analysis": {"keywords": []},
                },
            },
            status=SurveyAIReportSnapshot.Status.SNAPSHOT_READY,
            response_count=1,
        )

    @skipUnlessDBFeature("has_select_for_update_skip_locked")
    def test_two_workers_claim_one_pending_job_exactly_once(self):
        scheduled = schedule_survey_analysis(self.survey.pk)

        results = self._claim_together(("postgres-worker-a", "postgres-worker-b"))

        claimed = [result for result in results if result is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0][0], scheduled.pk)
        scheduled.refresh_from_db()
        self.assertEqual(scheduled.status, AnalysisJob.Status.RUNNING)
        self.assertEqual(scheduled.attempt_count, 1)

    @skipUnlessDBFeature("has_select_for_update_skip_locked")
    def test_expired_lease_has_one_successor_and_old_worker_cannot_publish(self):
        scheduled = schedule_survey_analysis(self.survey.pk)
        first = claim_next_job("postgres-old-worker", lease_seconds=30)
        old_token = first.lease_token
        AnalysisJob.objects.filter(pk=scheduled.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )

        results = self._claim_together(("postgres-new-worker-a", "postgres-new-worker-b"))

        successors = [result for result in results if result is not None]
        self.assertEqual(len(successors), 1)
        successor_job_id, successor_token = successors[0]
        self.assertEqual(successor_job_id, scheduled.pk)
        self.assertNotEqual(successor_token, old_token)

        snapshot = self._make_snapshot("q")
        with self.assertRaises(AnalysisJobError):
            publish_analysis_snapshot(scheduled.pk, old_token, snapshot_id=snapshot.pk)

        result = publish_analysis_snapshot(scheduled.pk, successor_token, snapshot_id=snapshot.pk)
        self.assertTrue(result.published)
        state = SurveyAnalysisState.objects.get(survey=self.survey)
        self.assertEqual(state.published_snapshot_id, snapshot.pk)
        scheduled.refresh_from_db()
        self.assertEqual(scheduled.status, AnalysisJob.Status.SUCCEEDED)
        self.assertEqual(scheduled.attempt_count, 2)

    @skipUnlessDBFeature("has_select_for_update_skip_locked")
    def test_source_version_switch_rejects_old_worker_publication(self):
        first_sha = "a" * 64
        _, first_version, first_job, _ = register_external_dataset_version(
            self.survey.pk,
            source_ref="fixture/reviews",
            source_version=f"revision-a:clean-v1:{first_sha}",
            source_revision="revision-a",
            cleaning_version="clean-v1",
            content_sha256=first_sha,
            mapping_key="fixture_mapping",
            mapping_version="fixture-v1",
            row_count=4,
        )
        first = claim_next_job("postgres-old-worker", lease_seconds=30)
        self.assertEqual(first.pk, first_job.pk)
        second_sha = "b" * 64
        _, second_version, _, _ = register_external_dataset_version(
            self.survey.pk,
            source_ref="fixture/reviews",
            source_version=f"revision-b:clean-v1:{second_sha}",
            source_revision="revision-b",
            cleaning_version="clean-v1",
            content_sha256=second_sha,
            mapping_key="fixture_mapping",
            mapping_version="fixture-v1",
            row_count=5,
        )

        result = publish_analysis_snapshot(
            first.pk,
            first.lease_token,
            snapshot_id=self._make_snapshot("r").pk,
        )

        self.assertTrue(result.stale)
        first.refresh_from_db()
        self.assertEqual((first.status, first.error_code), (AnalysisJob.Status.CANCELLED, "superseded"))
        self.assertFalse(
            AnalysisJob.objects.filter(
                status=AnalysisJob.Status.PENDING,
                source_version=first_version.source_version,
            ).exists()
        )
        self.assertTrue(
            AnalysisJob.objects.filter(
                status=AnalysisJob.Status.PENDING,
                source_version=second_version.source_version,
            ).exists()
        )
