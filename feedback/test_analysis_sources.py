import hashlib
import json
from pathlib import Path
import tempfile

from django.core.management import call_command
from django.test import TestCase

from .analysis_jobs import check_claim_current, claim_next_job, schedule_survey_analysis
from .analysis_sources import register_external_dataset_version, resolve_analysis_source
from .models import AnalysisJob, Survey, SurveyAnalysisState


class AnalysisSourceRegistrationTests(TestCase):
    def setUp(self):
        self.survey = Survey.objects.create(title="Source fixture", slug="source-fixture")

    def register(self, marker="a"):
        return register_external_dataset_version(
            self.survey.pk,
            source_ref="fixture/reviews",
            source_version=f"revision-{marker}:clean-v1:{marker * 64}",
            source_revision=f"revision-{marker}",
            cleaning_version="clean-v1",
            content_sha256=marker * 64,
            schema_sha256="b" * 64,
            mapping_key="fixture_mapping",
            mapping_version="fixture-v1",
            row_count=4,
            provenance={"dataset_url": "https://example.test/dataset"},
        )

    def test_external_registration_is_the_authoritative_job_source(self):
        source, version, job, changed = self.register()

        binding = resolve_analysis_source(self.survey)
        self.assertTrue(changed)
        self.assertEqual(source.active_external_version, version)
        self.assertEqual(binding.source_ref, "fixture/reviews")
        self.assertEqual(job.source_kind, AnalysisJob.SourceKind.EXTERNAL)
        self.assertEqual(job.source_version, binding.source_version)
        state = SurveyAnalysisState.objects.get(survey=self.survey)
        self.assertEqual((state.input_version, state.config_version), (1, 1))

    def test_source_switch_rejects_old_worker_and_queues_only_new_version(self):
        _, first_version, first_job, _ = self.register("a")
        claimed = claim_next_job("old-worker", lease_seconds=30)
        self.assertEqual(claimed.pk, first_job.pk)

        _, second_version, _, changed = self.register("c")

        self.assertTrue(changed)
        self.assertFalse(check_claim_current(claimed.pk, claimed.lease_token))
        claimed.refresh_from_db()
        self.assertEqual((claimed.status, claimed.error_code), (AnalysisJob.Status.CANCELLED, "superseded"))
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

    def test_unregistered_external_job_cannot_bypass_source_registry(self):
        with self.assertRaisesMessage(ValueError, "目前登錄"):
            schedule_survey_analysis(
                self.survey.pk,
                source_kind=AnalysisJob.SourceKind.EXTERNAL,
                source_ref="fixture/reviews",
                source_version="revision-a:clean-v1:" + "a" * 64,
            )

    def test_switching_to_external_marks_answer_publication_not_current(self):
        answer_job = schedule_survey_analysis(self.survey.pk, change="input")
        state = SurveyAnalysisState.objects.get(survey=self.survey)
        state.publication_manifest = {
            "statistics": {
                "input_version": answer_job.input_version,
                "config_version": answer_job.config_version,
                "pipeline_version": answer_job.pipeline_version,
                "source_kind": AnalysisJob.SourceKind.ANSWERS,
                "source_ref": "",
                "source_version": "",
            }
        }
        state.save(update_fields=("publication_manifest",))

        self.register()

        from .published_analysis import _stage_is_current

        state.refresh_from_db()
        self.assertFalse(_stage_is_current(state, "statistics"))

    def test_registration_command_hashes_local_artifact_before_persisting_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clean = root / "clean" / "fixture.parquet"
            clean.parent.mkdir()
            clean.write_bytes(b"fixture parquet bytes")
            digest = hashlib.sha256(clean.read_bytes()).hexdigest()
            manifest = root / "manifest" / "dataset-manifest.json"
            manifest.parent.mkdir()
            manifest.write_text(
                json.dumps(
                    {
                        "source": {"dataset": "fixture/reviews", "source_revision": "revision-a"},
                        "clean_path": "clean/fixture.parquet",
                        "cleaning_version": "clean-v1",
                        "counts": {"retained_rows": 4},
                        "artifacts": [
                            {"path": "clean/fixture.parquet", "size": clean.stat().st_size, "sha256": digest}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            mapping = root / "fixture_mapping.json"
            mapping.write_text(
                json.dumps(
                    {
                        "mapping_version": "fixture-v1",
                        "dataset": {"name": "fixture/reviews", "version": "revision-a"},
                        "questions": [],
                    }
                ),
                encoding="utf-8",
            )

            call_command(
                "register_external_analysis_source",
                survey=self.survey.slug,
                manifest=str(manifest),
                mapping=str(mapping),
            )

        binding = resolve_analysis_source(self.survey)
        self.assertEqual(binding.source_ref, "fixture/reviews")
        self.assertEqual(binding.mapping_key, "fixture_mapping")
