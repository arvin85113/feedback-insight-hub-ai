import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import duckdb
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.db import connection

from desktop_app.service import DesktopService, DesktopServiceError, DesktopTaskCancelled
from feedback.importing.local_dataset import file_sha256


class DesktopServiceTests(SimpleTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        clean = self.root / "clean" / "fixture.parquet"
        clean.parent.mkdir()
        rows = [
            (4, 5, "clean comfortable room and friendly staff", 39),
            (2, None, "the room was not good", 21),
            (5, 4, "excellent service and clean room", 32),
        ]
        with duckdb.connect(":memory:") as connection:
            connection.execute(
                "CREATE TABLE fixture(overall INTEGER, rooms INTEGER, text VARCHAR, review_length INTEGER)"
            )
            connection.executemany("INSERT INTO fixture VALUES (?, ?, ?, ?)", rows)
            connection.execute(f"COPY fixture TO '{clean.as_posix()}' (FORMAT PARQUET)")

        source_revision = "a" * 40
        manifest_dir = self.root / "manifest"
        manifest_dir.mkdir()
        self.manifest = manifest_dir / "dataset-manifest.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "source": {
                        "dataset": "fixture/reviews",
                        "source_revision": source_revision,
                        "viewer_conversion_revision": "b" * 40,
                    },
                    "cleaning_version": "fixture-clean-v1",
                    "counts": {"input_rows": 3, "retained_rows": 3},
                    "clean_path": "clean/fixture.parquet",
                    "artifacts": [
                        {
                            "path": "clean/fixture.parquet",
                            "size": clean.stat().st_size,
                            "sha256": file_sha256(clean),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.mapping = self.root / "mapping.json"
        self.mapping.write_text(
            json.dumps(
                {
                    "dataset": {"name": "fixture/reviews", "version": source_revision},
                    "questions": [
                        {
                            "source_field": "overall",
                            "title": "整體評分",
                            "kind": "scale",
                            "data_type": "ordinal",
                            "required": True,
                            "options": ["1", "2", "3", "4", "5"],
                        },
                        {
                            "source_field": "rooms",
                            "title": "客房評分",
                            "kind": "scale",
                            "data_type": "ordinal",
                            "required": False,
                            "options": ["1", "2", "3", "4", "5"],
                        },
                        {
                            "source_field": "text",
                            "title": "評論",
                            "kind": "long_text",
                            "data_type": "text",
                            "required": True,
                            "options": [],
                            "enable_keyword_tracking": True,
                        },
                        {
                            "source_field": "text",
                            "title": "評論長度",
                            "kind": "integer",
                            "data_type": "discrete",
                            "required": True,
                            "normalizers": ["text_length"],
                            "options": [],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.service = DesktopService(
            project_root=self.root,
            dataset_root=self.root,
            manifest_path=self.manifest,
            mapping_path=self.mapping,
            output_root=self.root / "analysis",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_inspection_and_analysis_reuse_verified_local_service(self):
        events = []
        status = self.service.inspect_dataset(progress=lambda stage, percent: events.append((stage, percent)))
        self.assertTrue(status.verified)
        self.assertEqual(status.retained_rows, 3)
        self.assertEqual(events[-1][1], 100)

        summary = self.service.analyze()
        self.assertFalse(summary.cache_hit)
        self.assertEqual(summary.input_rows, 3)
        self.assertEqual(summary.ai_mode, "mock")
        self.assertTrue(Path(summary.result_path).is_file())
        self.assertTrue(self.service.analyze().cache_hit)

    def test_inspection_rejects_user_id_question(self):
        mapping = json.loads(self.mapping.read_text(encoding="utf-8"))
        mapping["questions"].append(
            {
                "source_field": "user_id",
                "title": "使用者",
                "kind": "short_text",
                "data_type": "text",
                "required": False,
            }
        )
        self.mapping.write_text(json.dumps(mapping), encoding="utf-8")
        with self.assertRaisesMessage(DesktopServiceError, "不得包含 user_id"):
            self.service.inspect_dataset()

    def test_cancelled_inspection_stops_before_analysis(self):
        with self.assertRaises(DesktopTaskCancelled):
            self.service.inspect_dataset(cancel_requested=lambda: True)
        self.assertFalse(self.service.output_root.exists())


class DesktopDatabaseServiceTests(TestCase):
    def setUp(self):
        from feedback.analysis_jobs import schedule_survey_analysis, suppress_analysis_scheduling
        from feedback.models import Answer, FeedbackSubmission, Question, Survey

        self.temporary = tempfile.TemporaryDirectory()
        self.survey = Survey.objects.create(title="資料庫問卷", slug="database-survey")
        with suppress_analysis_scheduling():
            rating = Question.objects.create(
                survey=self.survey,
                title="整體評分",
                kind=Question.Kind.SCALE,
                data_type=Question.DataType.ORDINAL,
                options_text="1\n2\n3\n4\n5",
                order=1,
            )
            comment = Question.objects.create(
                survey=self.survey,
                title="評論",
                kind=Question.Kind.LONG_TEXT,
                data_type=Question.DataType.TEXT,
                enable_keyword_tracking=True,
                order=2,
            )
            for index in range(3):
                submission = FeedbackSubmission.objects.create(survey=self.survey)
                Answer.objects.bulk_create(
                    [
                        Answer(submission=submission, question=rating, value=str(index + 3)),
                        Answer(submission=submission, question=comment, value="clean friendly room"),
                    ]
                )
        schedule_survey_analysis(self.survey.pk, change="input")
        self.service = DesktopService(project_root=self.temporary.name)
        self.service.database_output_root = Path(self.temporary.name) / "database-analysis"

    def tearDown(self):
        self.temporary.cleanup()

    def test_lists_database_survey_and_marks_unpublished_data_stale(self):
        status = self.service.list_surveys()[0]
        self.assertEqual(status.survey_id, self.survey.pk)
        self.assertEqual(status.response_count, 3)
        self.assertEqual(status.question_count, 2)
        self.assertEqual(status.source, "線上問卷")
        self.assertTrue(status.needs_update)
        self.assertIsNotNone(status.latest_data_at)

    def test_selected_update_publishes_and_marks_statistics_and_text_current(self):
        result = self.service.update_surveys((self.survey.pk,))
        self.assertEqual(result.updated_count, 1)
        self.assertEqual(result.input_rows, 3)
        status = self.service.list_surveys()[0]
        self.assertFalse(status.needs_update)
        self.assertTrue(status.statistics_current)
        self.assertTrue(status.text_current)
        self.assertIsNotNone(status.latest_analysis_at)
        self.assertTrue(status.needs_ai)
        self.assertFalse(status.ai_current)
        self.assertIsNone(status.latest_ai_at)

    @patch("feedback.ai_worker.execute_ai_job")
    @override_settings(AI_REPORT_REQUEST_INTERVAL_SECONDS=0)
    def test_two_stage_update_publishes_new_ai_stage_without_replacing_snapshot(
        self, execute_ai_job
    ):
        from feedback.analysis_jobs import publish_analysis_stages
        from feedback.models import (
            SurveyAIAnalysisStage,
            SurveyAIReportSnapshot,
            SurveyAnalysisState,
        )

        def publish_fixture(job, *, allow_paid_ai, lease_seconds):
            self.assertTrue(allow_paid_ai)
            snapshot = SurveyAnalysisState.objects.get(survey=self.survey).published_snapshot
            stage = SurveyAIAnalysisStage.objects.create(
                snapshot=snapshot,
                stage_type=SurveyAIAnalysisStage.StageType.SYNTHESIS,
                status=SurveyAIAnalysisStage.Status.SUCCEEDED,
                input_hash="a" * 64,
                schema_version="desktop-test-v1",
                prompt_version="desktop-test-v1",
                model_name="gemini-test",
                output_json={
                    "executive_summary": "測試分析",
                    "combined_findings": [],
                    "improvement_drafts": [],
                    "data_caveats": [],
                },
            )
            publish_analysis_stages(
                job.pk,
                job.lease_token,
                snapshot_id=snapshot.pk,
                stage_ids={SurveyAIAnalysisStage.StageType.SYNTHESIS: stage.pk},
                input_fingerprint=snapshot.data_fingerprint,
            )

        execute_ai_job.side_effect = publish_fixture

        result = self.service.update_two_stage_analysis(
            (self.survey.pk,),
            include_ai=True,
        )

        self.assertEqual(result.deterministic.updated_count, 1)
        self.assertEqual(result.ai.updated_count, 1)
        execute_ai_job.assert_called_once()
        self.assertEqual(SurveyAIReportSnapshot.objects.filter(survey=self.survey).count(), 1)
        self.assertEqual(
            SurveyAIAnalysisStage.objects.filter(
                snapshot__survey=self.survey,
                stage_type=SurveyAIAnalysisStage.StageType.SYNTHESIS,
            ).count(),
            1,
        )
        status = self.service.list_surveys()[0]
        self.assertFalse(status.needs_update)
        self.assertFalse(status.needs_ai)
        self.assertTrue(status.ai_current)
        self.assertIsNotNone(status.latest_ai_at)

    @patch("feedback.ai_stage_service.create_gemini_client")
    def test_second_stage_requires_explicit_paid_ai_enablement(self, client_factory):
        self.service.update_surveys((self.survey.pk,))

        with self.assertRaisesMessage(DesktopServiceError, "Gemini 尚未啟用"):
            self.service.update_ai_surveys((self.survey.pk,))

        client_factory.assert_not_called()

    def test_new_input_creates_another_snapshot_and_keeps_previous_result(self):
        from feedback.models import Answer, FeedbackSubmission, SurveyAIReportSnapshot

        self.service.update_surveys((self.survey.pk,))
        first_snapshot = SurveyAIReportSnapshot.objects.get(survey=self.survey)
        submission = FeedbackSubmission.objects.create(survey=self.survey)
        for question in self.survey.questions.all():
            value = "5" if question.data_type == question.DataType.ORDINAL else "new clean review"
            Answer.objects.create(submission=submission, question=question, value=value)

        self.service.update_surveys((self.survey.pk,))

        snapshots = SurveyAIReportSnapshot.objects.filter(survey=self.survey)
        self.assertEqual(snapshots.count(), 2)
        self.assertTrue(snapshots.filter(pk=first_snapshot.pk).exists())
        self.assertNotEqual(
            self.survey.analysis_state.published_snapshot_id,
            first_snapshot.pk,
        )

    def test_answer_adapter_streams_database_rows_without_identifying_fields(self):
        from feedback.analysis_adapters import AnswerInput

        adapter = AnswerInput.from_survey(self.survey, version="fixture-v1")
        self.assertIsNone(adapter.rows)
        columns = tuple(field.name for field in adapter.fields())
        rows = list(adapter.scan(columns))
        self.assertEqual(len(rows), 3)
        self.assertEqual(set(rows[0]), set(columns))
        self.assertNotIn("respondent_email", repr(rows))

    def test_empty_column_scan_counts_rows_without_selecting_identity_fields(self):
        from feedback.analysis_adapters import AnswerInput

        adapter = AnswerInput.from_survey(self.survey, version="fixture-v1")
        with CaptureQueriesContext(connection) as queries:
            rows = list(adapter.scan(()))
        self.assertEqual(rows, [{}, {}, {}])
        sql = " ".join(item["sql"] for item in queries.captured_queries).lower()
        self.assertNotIn("respondent_email", sql)
        self.assertNotIn("respondent_name", sql)
