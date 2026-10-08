from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone

from feedback.analysis_jobs import claim_next_job
from feedback.job_progress import report_job_progress
from feedback.models import AnalysisJob, Survey
from node.job_display import describe_job


class JobProgressTests(TestCase):
    def setUp(self):
        survey = Survey.objects.create(title="Progress fixture", slug="progress-fixture")
        self.job = AnalysisJob.objects.create(survey=survey, status="running", executor="deterministic",
            input_version=1, config_version=1, pipeline_version="fixture", attempt_count=1,
            lease_token=uuid.uuid4(), lease_expires_at=timezone.now() + timedelta(minutes=5))

    def report(self, **kwargs):
        return report_job_progress(self.job.pk, self.job.lease_token, phase="text", completed=1, **kwargs)

    def test_checkpoint_is_bounded_and_does_not_touch_result_identity(self):
        AnalysisJob.objects.filter(pk=self.job.pk).update(result_manifest={"existing": "keep"})
        self.assertTrue(self.report())
        self.job.refresh_from_db()
        self.assertEqual(self.job.result_manifest["existing"], "keep")
        self.assertEqual(self.job.result_manifest["_progress"]["completed"], 1)
        self.assertEqual(describe_job(self.job).ui_progress_percent, 25)
        self.assertEqual(self.job.status, "running")
        self.assertEqual(self.job.input_version, 1)

    def test_old_token_expired_and_cancelled_workers_cannot_write(self):
        self.assertFalse(report_job_progress(self.job.pk, uuid.uuid4(), phase="text", completed=1))
        AnalysisJob.objects.filter(pk=self.job.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertFalse(self.report())
        AnalysisJob.objects.filter(pk=self.job.pk).update(lease_expires_at=timezone.now() + timedelta(minutes=5),
                                                          cancel_requested_at=timezone.now())
        self.assertFalse(self.report())
        self.job.refresh_from_db()
        self.assertEqual(self.job.result_manifest, {})

    def test_progress_cannot_regress_in_the_same_attempt(self):
        self.assertTrue(self.report())
        self.assertFalse(report_job_progress(self.job.pk, self.job.lease_token, phase="prepare", completed=0))

    def test_success_only_is_100_and_not_writable(self):
        AnalysisJob.objects.filter(pk=self.job.pk).update(status="succeeded")
        self.assertFalse(self.report())
        self.job.refresh_from_db()
        self.assertEqual(describe_job(self.job).ui_progress_percent, 100)

    def test_wrong_phase_invalid_count_and_untrusted_text_are_rejected(self):
        for phase, done in (("ai_text", 1), ("arbitrary private text", 1), ("text", True), ("text", 4)):
            with self.assertRaises(ValueError):
                report_job_progress(self.job.pk, self.job.lease_token, phase=phase, completed=done)

    def test_reclaimed_attempt_drops_old_progress_and_rejects_old_owner(self):
        self.report()
        old_token = self.job.lease_token
        AnalysisJob.objects.filter(pk=self.job.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        reclaimed = claim_next_job("fixture-reclaim", job_ids=[self.job.pk], lease_seconds=60)
        self.assertEqual(reclaimed.attempt_count, 2)
        self.assertNotIn("_progress", reclaimed.result_manifest)
        self.assertFalse(report_job_progress(self.job.pk, old_token, phase="text", completed=1))
        self.assertTrue(report_job_progress(self.job.pk, reclaimed.lease_token, phase="prepare", completed=0))

    def test_legacy_running_unknown_failed_and_pending_states(self):
        shown = describe_job(self.job)
        self.assertFalse(shown.ui_progress_known)
        self.assertTrue(shown.ui_progress_busy)
        self.job.status = "failed"
        shown = describe_job(self.job)
        self.assertFalse(shown.ui_progress_busy)
        self.assertIn("失敗", shown.ui_progress_phase)
        self.job.status = "pending"
        shown = describe_job(self.job)
        self.assertEqual(shown.ui_progress_percent, 0)
        self.assertTrue(shown.ui_progress_known)

    def test_checkpoint_from_an_old_attempt_is_not_presented(self):
        self.job.result_manifest = {"_progress": {"version": 1, "attempt": 0, "phase": "text", "completed": 1, "total": 4}}
        self.assertFalse(describe_job(self.job).ui_progress_known)
