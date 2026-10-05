from io import StringIO
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from cloudsync.definitions import upsert_definition
from cloudsync.models import ResultUpload
from cloudsync.tests.test_inbox import definition
from feedback.analysis_worker import WorkerExecutionError
from feedback.analysis_jobs import schedule_survey_analysis
from feedback.external_dataset import validate_external_dataset
from feedback.models import AnalysisJob, ExternalDatasetVersion, Survey, SurveyAnalysisState
from feedback.test_external_dataset import dataset_fixture
from node.datasets import input_for_job, register_local_dataset
from node.models import LocalDatasetLocation


class LocalDatasetTests(TestCase):
    def setUp(self):
        self.survey, _ = upsert_definition(definition(1))
        self.work = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.manifest, self.mapping, self.clean = dataset_fixture(self.root)
        self.verified = validate_external_dataset(self.manifest, self.mapping)

    def register(self):
        return register_local_dataset(self.survey.pk, self.verified)

    def test_reregistration_reuses_version_location_and_job(self):
        _, first, job, changed = self.register()
        count = AnalysisJob.objects.count()
        _, again, second_job, changed_again = self.register()
        self.assertTrue(changed)
        self.assertEqual(first.pk, again.pk)
        self.assertIsNone(second_job)
        self.assertFalse(changed_again)
        self.assertEqual((LocalDatasetLocation.objects.count(), AnalysisJob.objects.count()), (1, count))
        self.assertEqual(input_for_job(job).manifest_path, self.manifest)

    def test_version_and_locator_roll_back_together(self):
        with patch.object(LocalDatasetLocation.objects, "update_or_create", side_effect=RuntimeError("fixture")):
            with self.assertRaises(RuntimeError):
                self.register()
        self.assertFalse(ExternalDatasetVersion.objects.exists())
        self.assertFalse(AnalysisJob.objects.filter(source_kind="external").exists())

    def test_dry_run_creates_no_locator_version_or_job(self):
        before = AnalysisJob.objects.count()
        call_command("register_external_analysis_source", survey=self.survey.slug,
                     manifest=str(self.manifest), mapping=str(self.mapping), dry_run=True, stdout=StringIO())
        self.assertFalse(LocalDatasetLocation.objects.exists())
        self.assertFalse(ExternalDatasetVersion.objects.exists())
        self.assertEqual(AnalysisJob.objects.count(), before)

    def test_missing_or_changed_file_and_mapping_do_not_fall_back(self):
        _, _, job, _ = self.register()
        self.clean.write_bytes(b"bad")
        with self.assertRaises(WorkerExecutionError) as error:
            input_for_job(job)
        self.assertEqual(error.exception.code, "external_location_integrity_mismatch")
        self.clean.unlink()
        with self.assertRaises(WorkerExecutionError):
            input_for_job(job)

    def test_mapping_edit_is_rejected_even_if_clean_hash_matches(self):
        _, _, job, _ = self.register()
        body = json.loads(self.mapping.read_text(encoding="utf-8"))
        body["questions"][0]["title"] = "Changed"
        self.mapping.write_text(json.dumps(body), encoding="utf-8")
        with self.assertRaises(WorkerExecutionError):
            input_for_job(job)

    def test_old_job_resolves_its_own_locator_not_the_current_version(self):
        _, first, old_job, _ = self.register()
        manifest, mapping, _ = dataset_fixture(self.root, "second")
        _, second, _, _ = register_local_dataset(self.survey.pk, validate_external_dataset(manifest, mapping))
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(input_for_job(old_job).manifest_path, self.manifest)

    def test_worker_without_cli_paths_publishes_bounded_external_upload(self):
        self.register()
        call_command("run_analysis_worker", worker_id="fixture", output=str(self.root / "output"),
                     once=True, stdout=StringIO())
        upload = ResultUpload.objects.get(survey=self.survey)
        self.assertEqual(upload.content["input_source"]["source_version"], self.verified.registration["source_version"])
        self.assertEqual(upload.content["definition_version"], 1)
        self.assertEqual(upload.content["coverage"]["analyzed_unique"], 4)
        self.assertEqual(upload.content["analyzed_through_sequence"], 0)
        encoded = json.dumps(upload.content)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn("user_id", encoded)

    def test_missing_locator_job_is_claimed_and_fails_explicitly(self):
        self.register()
        # A missing locator is not a missing dataset version. Use raw delete only in this isolated fixture.
        LocalDatasetLocation.objects.all().delete()
        call_command("run_analysis_worker", worker_id="fixture", output=str(self.root / "output"),
                     once=True, stdout=StringIO())
        job = AnalysisJob.objects.get(source_kind="external")
        self.assertEqual((job.status, job.error_code), ("failed", "external_location_missing"))
        self.assertFalse(ResultUpload.objects.exists())

    def test_integrity_failure_keeps_last_published_snapshot_and_upload(self):
        self.register()
        options = dict(worker_id="fixture", output=str(self.root / "output"), once=True, stdout=StringIO())
        call_command("run_analysis_worker", **options)
        previous = SurveyAnalysisState.objects.get(survey=self.survey).published_snapshot_id
        previous_upload = ResultUpload.objects.get(survey=self.survey).publish_uuid
        self.clean.write_bytes(b"changed")
        schedule_survey_analysis(self.survey.pk, change="none")
        call_command("run_analysis_worker", **options)
        self.assertEqual(SurveyAnalysisState.objects.get(survey=self.survey).published_snapshot_id, previous)
        self.assertEqual(ResultUpload.objects.get(survey=self.survey).publish_uuid, previous_upload)
        self.assertTrue(AnalysisJob.objects.filter(status="failed", error_code="external_location_integrity_mismatch").exists())


class LocalDatasetMigrationTests(TransactionTestCase):
    def test_locator_migration_is_reversible_without_deleting_dataset_versions(self):
        from feedback.analysis_sources import register_external_dataset_version

        survey = Survey.objects.create(title="Rollback fixture", slug="rollback-fixture")
        _, version, _, _ = register_external_dataset_version(
            survey.pk, source_ref="fixture/reviews", source_version="v1", source_revision="revision-v1",
            cleaning_version="clean-v1", content_sha256="a" * 64, mapping_key="fixture",
            mapping_version="v1", row_count=4,
        )
        LocalDatasetLocation.objects.create(version=version, manifest_path="fixture", mapping_path="fixture",
                                            manifest_sha256="b" * 64, mapping_sha256="c" * 64)
        try:
            MigrationExecutor(connection).migrate([("node", "0001_initial")])
            self.assertNotIn(LocalDatasetLocation._meta.db_table, connection.introspection.table_names())
            self.assertTrue(ExternalDatasetVersion.objects.filter(pk=version.pk).exists())
            self.assertTrue(Survey.objects.filter(pk=survey.pk).exists())
        finally:
            MigrationExecutor(connection).migrate([("node", "0002_localdatasetlocation")])
        self.assertFalse(LocalDatasetLocation.objects.exists())
