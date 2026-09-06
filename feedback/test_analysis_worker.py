import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

import duckdb

from .analysis_jobs import claim_next_job, schedule_survey_analysis, suppress_analysis_scheduling
from .analysis_worker import ExternalInputSpec, execute_deterministic_job
from .importing.local_dataset import file_sha256
from .models import (
    AnalysisJob,
    Answer,
    FeedbackSubmission,
    Question,
    Survey,
    SurveyAIReportSnapshot,
    SurveyAnalysisState,
)


class DeterministicWorkerTests(TestCase):
    def setUp(self):
        self.survey = Survey.objects.create(title="Worker fixture", slug="worker-fixture")
        with suppress_analysis_scheduling():
            self.rating = Question.objects.create(
                survey=self.survey,
                title="Rating",
                kind=Question.Kind.SCALE,
                data_type=Question.DataType.ORDINAL,
                options_text="1\n2\n3\n4\n5",
                order=1,
            )
            self.comment = Question.objects.create(
                survey=self.survey,
                title="Comment",
                kind=Question.Kind.LONG_TEXT,
                data_type=Question.DataType.TEXT,
                enable_keyword_tracking=True,
                order=2,
            )
            for index in range(4):
                submission = FeedbackSubmission.objects.create(survey=self.survey)
                Answer.objects.bulk_create(
                    [
                        Answer(submission=submission, question=self.rating, value=str(index % 5 + 1)),
                        Answer(
                            submission=submission,
                            question=self.comment,
                            value="ZeldaSecret zelda@example.com clean friendly room",
                        ),
                    ]
                )

    def queue_and_claim(self, worker_id="worker-test"):
        schedule_survey_analysis(self.survey.pk, change="input")
        return claim_next_job(
            worker_id,
            lease_seconds=60,
            executor=AnalysisJob.Executor.DETERMINISTIC,
            external_source_refs=(),
        )

    def test_native_job_publishes_bounded_snapshot_without_raw_text_and_reuses_cache(self):
        claimed = self.queue_and_claim()
        with tempfile.TemporaryDirectory() as directory, patch(
            "feedback.ai_report_service.create_gemini_client",
            side_effect=AssertionError("Gemini must not be called"),
        ):
            first = execute_deterministic_job(claimed, output_root=directory, lease_seconds=60)
            self.assertTrue(first.published)
            self.assertFalse(first.cache_hit)
            raw = first.artifact_path.read_text(encoding="utf-8")
            self.assertNotIn("ZeldaSecret", raw)
            self.assertNotIn("zelda@example.com", raw)
            state = SurveyAnalysisState.objects.get(survey=self.survey)
            self.assertEqual(state.published_snapshot_id, first.snapshot_id)
            self.assertIn("display_payload", state.published_snapshot.source_snapshot)
            self.assertTrue(state.published_display_payload["statistics"]["charts"])
            self.assertNotIn("ZeldaSecret", repr(state.published_display_payload))
            self.assertNotIn("zelda@example.com", repr(state.published_display_payload))
            schedule_survey_analysis(self.survey.pk, change="none")
            second_claim = claim_next_job(
                "worker-test",
                lease_seconds=60,
                executor=AnalysisJob.Executor.DETERMINISTIC,
                external_source_refs=(),
            )
            second = execute_deterministic_job(second_claim, output_root=directory, lease_seconds=60)
            self.assertTrue(second.cache_hit)
            self.assertEqual(second.artifact_path, first.artifact_path)
            self.assertEqual(SurveyAIReportSnapshot.objects.count(), 1)

    def test_source_change_before_publish_keeps_artifact_but_rejects_pointer_update(self):
        claimed = self.queue_and_claim()
        from . import analysis_worker

        original = analysis_worker._persist_snapshot

        def persist_then_invalidate(job, result):
            snapshot = original(job, result)
            schedule_survey_analysis(self.survey.pk, change="input")
            return snapshot

        with tempfile.TemporaryDirectory() as directory, patch(
            "feedback.analysis_worker._persist_snapshot",
            side_effect=persist_then_invalidate,
        ):
            result = execute_deterministic_job(claimed, output_root=directory, lease_seconds=60)
            self.assertFalse(result.published)
            self.assertTrue(result.stale)
            self.assertTrue(result.artifact_path.exists())
            self.assertIsNone(SurveyAnalysisState.objects.get(survey=self.survey).published_snapshot)
            claimed.refresh_from_db()
            self.assertEqual((claimed.status, claimed.error_code), (AnalysisJob.Status.CANCELLED, "superseded"))
            self.assertTrue(AnalysisJob.objects.filter(status=AnalysisJob.Status.PENDING).exists())

    def test_command_failure_does_not_publish_partial_result(self):
        self.queue_and_claim()
        # Return the claimed row to pending so the command owns the lease it acquires.
        AnalysisJob.objects.update(
            status=AnalysisJob.Status.PENDING,
            worker_id="",
            lease_token=None,
            lease_expires_at=None,
            heartbeat_at=None,
            attempt_count=0,
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "feedback.analysis_worker.calculate_text",
            side_effect=RuntimeError("private failure detail"),
        ):
            with self.assertRaises(CommandError):
                call_command(
                    "run_analysis_worker_once",
                    worker_id="command-worker",
                    output=directory,
                    lease_seconds=60,
                )
            job = AnalysisJob.objects.get()
            self.assertEqual(job.status, AnalysisJob.Status.FAILED)
            self.assertEqual(job.error_code, "worker_runtimeerror")
            self.assertFalse(SurveyAIReportSnapshot.objects.exists())
            self.assertFalse(list(Path(directory).glob("*.json")))
            self.assertFalse(list(Path(directory).glob("*.lock")))

    def test_command_is_idle_when_no_supported_job_exists(self):
        schedule_survey_analysis(
            self.survey.pk,
            change="none",
            requested_stages=("synthesis",),
        )
        with tempfile.TemporaryDirectory() as directory:
            call_command(
                "run_analysis_worker_once",
                worker_id="command-worker",
                output=directory,
                lease_seconds=60,
            )
        self.assertEqual(AnalysisJob.objects.get().executor, AnalysisJob.Executor.AI)
        self.assertEqual(AnalysisJob.objects.get().status, AnalysisJob.Status.PENDING)

    def test_external_parquet_job_uses_registered_fixed_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "clean").mkdir()
            (root / "manifest").mkdir()
            parquet = root / "clean" / "fixture.parquet"
            with duckdb.connect(":memory:") as connection:
                connection.execute("CREATE TABLE fixture(overall INTEGER, text VARCHAR)")
                connection.executemany(
                    "INSERT INTO fixture VALUES (?, ?)",
                    [(5, "clean friendly room"), (4, "clean room"), (2, "not good"), (3, "friendly staff")],
                )
                connection.execute(f"COPY fixture TO '{parquet.as_posix()}' (FORMAT PARQUET)")
            parquet_hash = file_sha256(parquet)
            manifest = root / "manifest" / "dataset.json"
            manifest.write_text(
                json.dumps(
                    {
                        "clean_path": "clean/fixture.parquet",
                        "cleaning_version": "clean-v1",
                        "source": {"dataset": "fixture/reviews", "source_revision": "source-v1"},
                        "artifacts": [
                            {
                                "path": "clean/fixture.parquet",
                                "size": parquet.stat().st_size,
                                "sha256": parquet_hash,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            mapping = root / "mapping.json"
            mapping.write_text(
                json.dumps(
                    {
                        "dataset": {"name": "fixture/reviews", "version": "source-v1"},
                        "questions": [
                            {
                                "source_field": "overall",
                                "title": "Overall",
                                "kind": "scale",
                                "data_type": "ordinal",
                                "required": True,
                                "options": ["1", "2", "3", "4", "5"],
                            },
                            {
                                "source_field": "text",
                                "title": "Comment",
                                "kind": "long_text",
                                "data_type": "text",
                                "required": True,
                                "enable_keyword_tracking": True,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            source_version = f"source-v1:clean-v1:{parquet_hash}"
            schedule_survey_analysis(
                self.survey.pk,
                change="input",
                source_kind=AnalysisJob.SourceKind.EXTERNAL,
                source_ref="fixture",
                source_version=source_version,
            )
            claimed = claim_next_job(
                "external-worker",
                lease_seconds=60,
                executor=AnalysisJob.Executor.DETERMINISTIC,
                external_source_refs=("fixture",),
            )
            result = execute_deterministic_job(
                claimed,
                output_root=root / "output",
                external_inputs={"fixture": ExternalInputSpec(manifest, mapping)},
                lease_seconds=60,
            )
            self.assertTrue(result.published)
            self.assertEqual(result.input_rows, 4)
            self.assertEqual(
                SurveyAnalysisState.objects.get(survey=self.survey).publication_manifest["statistics"]["source_version"],
                source_version,
            )

    @override_settings(ANALYSIS_AUTO_AI_ENABLED=True)
    def test_successful_base_publication_queues_matching_ai_job(self):
        claimed = self.queue_and_claim()
        with tempfile.TemporaryDirectory() as directory:
            result = execute_deterministic_job(claimed, output_root=directory, lease_seconds=60)
        self.assertTrue(result.published)
        ai_job = AnalysisJob.objects.get(executor=AnalysisJob.Executor.AI)
        state = SurveyAnalysisState.objects.get(survey=self.survey)
        self.assertEqual(ai_job.status, AnalysisJob.Status.PENDING)
        self.assertEqual(
            (ai_job.input_version, ai_job.config_version, ai_job.pipeline_version),
            (state.input_version, state.config_version, state.pipeline_version),
        )
