import gzip
import io
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import duckdb
from django.core.management import call_command
from django.db import IntegrityError
from django.test import SimpleTestCase, TestCase

from feedback.importing.mapping import MappingConfigError, NormalizerSpec, load_mapping
from feedback.importing.normalizers import RowValidationError, normalize_value
from feedback.importing.readers import inspect_schema, iter_records
from feedback.importing.service import import_dataset
from feedback.local_service import get_survey_pandas_stats
from feedback.models import (
    AnalysisJob,
    Answer,
    DatasetImportBatch,
    FeedbackSubmission,
    ImportedSubmissionSource,
    Question,
    Survey,
    SurveyAnalysisState,
)


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
FIXTURE_DATA = FIXTURE_DIR / "external_import_test.jsonl"
FIXTURE_MAPPING = FIXTURE_DIR / "external_import_test_mapping.json"
AMAZON_MAPPING = (
    Path(__file__).resolve().parent
    / "import_mappings"
    / "amazon_beauty_reviews_2023.json"
)


class DatasetMappingAndNormalizerTests(SimpleTestCase):
    def test_fixture_mapping_supports_required_kinds_and_data_types(self):
        mapping = load_mapping(FIXTURE_MAPPING)
        self.assertEqual(mapping.dataset.name, "synthetic-import-test")
        self.assertEqual(
            {question.kind for question in mapping.questions},
            {"integer", "decimal", "scale", "single_choice", "short_text", "long_text"},
        )
        self.assertEqual(
            {question.data_type for question in mapping.questions},
            {"discrete", "continuous", "ordinal", "nominal", "text"},
        )

    def test_mapping_rejects_unknown_normalizer(self):
        raw = json.loads(FIXTURE_MAPPING.read_text(encoding="utf-8"))
        raw["questions"][0]["normalizers"] = ["eval"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(MappingConfigError):
                load_mapping(path)

        raw = json.loads(FIXTURE_MAPPING.read_text(encoding="utf-8"))
        raw["questions"][0]["source_field"] = "email"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(MappingConfigError):
                load_mapping(path)

    def test_mapping_rejects_sensitive_metadata_or_answer(self):
        raw = json.loads(FIXTURE_MAPPING.read_text(encoding="utf-8"))
        raw["metadata_fields"] = ["user_id"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(MappingConfigError):
                load_mapping(path)

    def test_amazon_mapping_preserves_review_text_and_protects_user_id(self):
        mapping = load_mapping(AMAZON_MAPPING)
        review = next(question for question in mapping.questions if question.source_field == "text")

        self.assertEqual(mapping.dataset.name, "jhan21/amazon-beauty-reviews-dataset")
        self.assertEqual(mapping.survey.title, "Amazon Beauty 2023 公開顧客評論分析")
        self.assertFalse(mapping.survey.is_active)
        self.assertIn("user_id", mapping.deduplication_fields)
        self.assertNotIn("user_id", mapping.metadata_fields)
        self.assertNotIn("images", mapping.metadata_fields)
        self.assertEqual(review.normalizers, ())

    def test_oversized_unmodified_text_is_rejected_instead_of_truncated(self):
        value = "x" * 50001
        with self.assertRaises(RowValidationError) as error:
            normalize_value(value, (), required=True)
        self.assertEqual(error.exception.code, "text_too_long")

    def test_normalizers_cover_boolean_decimal_length_and_invalid_integer(self):
        boolean = NormalizerSpec(
            "boolean",
            {
                "true_values": [True, "yes"],
                "false_values": [False, "no"],
                "true_label": "Yes",
                "false_label": "No",
            },
        )
        self.assertEqual(normalize_value("YES", (boolean,), required=True), "Yes")
        self.assertEqual(
            normalize_value("10.50", (NormalizerSpec("decimal", {}),), required=True),
            "10.5",
        )
        self.assertEqual(
            normalize_value(" text ", (NormalizerSpec("text_length", {}),), required=True),
            "6",
        )
        with self.assertRaises(RowValidationError):
            normalize_value("1.5", (NormalizerSpec("integer", {}),), required=True)


class DatasetReaderTests(SimpleTestCase):
    def test_schema_summary_never_contains_source_values(self):
        schema = inspect_schema(FIXTURE_DATA)
        encoded = json.dumps(schema)
        self.assertEqual(schema["row_count"], 10)
        self.assertNotIn("TEST-USER-001", encoded)
        self.assertNotIn("Synthetic fixture feedback", encoded)

    def test_csv_and_jsonl_gz_are_streamed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "sample.csv"
            csv_path.write_text("id,value\n1,hello\n", encoding="utf-8")
            self.assertEqual(list(iter_records(csv_path))[0].data["value"], "hello")

            gzip_path = root / "sample.jsonl.gz"
            with gzip.open(gzip_path, "wt", encoding="utf-8") as handle:
                handle.write('{"id": 1, "value": "hello"}\n')
            self.assertEqual(list(iter_records(gzip_path))[0].data["value"], "hello")

    def test_parquet_is_read_in_rows_without_converting_to_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            parquet_path = Path(directory) / "sample.parquet"
            with duckdb.connect(":memory:") as connection:
                connection.execute("CREATE TABLE sample(id INTEGER, value VARCHAR)")
                connection.execute("INSERT INTO sample VALUES (1, 'hello'), (2, 'world')")
                connection.execute(f"COPY sample TO '{parquet_path.as_posix()}' (FORMAT PARQUET)")
            records = list(iter_records(parquet_path))
            self.assertEqual([record.data["id"] for record in records], [1, 2])


class DatasetImportTests(TestCase):
    def setUp(self):
        self.mapping = load_mapping(FIXTURE_MAPPING)

    def _import(self):
        return import_dataset(
            FIXTURE_DATA,
            self.mapping,
            limit=50,
            seed=42,
            batch_size=3,
        )

    def test_schema_only_and_dry_run_write_nothing_and_hide_sensitive_values(self):
        output = io.StringIO()
        call_command(
            "import_feedback_dataset",
            input=str(FIXTURE_DATA),
            mapping=str(FIXTURE_MAPPING),
            schema_only=True,
            stdout=output,
        )
        self.assertEqual(Survey.objects.count(), 0)
        self.assertNotIn("TEST-USER", output.getvalue())
        self.assertNotIn("Synthetic fixture feedback", output.getvalue())

        output = io.StringIO()
        call_command(
            "import_feedback_dataset",
            input=str(FIXTURE_DATA),
            mapping=str(FIXTURE_MAPPING),
            dry_run=True,
            limit=50,
            seed=42,
            stdout=output,
        )
        self.assertEqual(Survey.objects.count(), 0)
        self.assertEqual(DatasetImportBatch.objects.count(), 0)
        self.assertIn("資料庫零寫入", output.getvalue())
        self.assertNotIn("TEST-USER", output.getvalue())

    def test_import_creates_expected_vertical_slice_and_timestamp(self):
        result = self._import()
        self.assertEqual(result.read_count, 10)
        self.assertEqual(result.valid_count, 8)
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(result.input_duplicate_count, 1)
        self.assertEqual(result.imported_count, 8)
        self.assertEqual(result.skip_reasons["invalid_integer"], 1)
        self.assertEqual(result.survey.questions.count(), 8)
        self.assertEqual(result.survey.submissions.count(), 8)
        self.assertEqual(ImportedSubmissionSource.objects.count(), 8)
        self.assertEqual(Answer.objects.count(), 63)
        state = SurveyAnalysisState.objects.get(survey=result.survey)
        self.assertEqual((state.input_version, state.config_version), (1, 1))
        self.assertEqual(AnalysisJob.objects.filter(survey=result.survey).count(), 1)

        source = ImportedSubmissionSource.objects.get(source_item_id="TEST-ITEM-001")
        self.assertEqual(source.source_timestamp, datetime(2024, 1, 1, tzinfo=UTC))
        self.assertEqual(source.submission.submitted_at, datetime(2024, 1, 1, tzinfo=UTC))
        self.assertFalse(source.submission.consent_follow_up)
        self.assertIsNone(source.submission.user)

    def test_question_contract_and_text_cache_are_populated(self):
        result = self._import()
        rating = result.survey.questions.get(title="Overall rating")
        detail = result.survey.questions.get(title="Feedback detail")
        self.assertEqual((rating.kind, rating.data_type), (Question.Kind.SCALE, Question.DataType.ORDINAL))
        self.assertEqual(
            (detail.kind, detail.data_type, detail.enable_keyword_tracking),
            (Question.Kind.LONG_TEXT, Question.DataType.TEXT, True),
        )
        answer = Answer.objects.filter(question=detail).first()
        self.assertTrue(answer.analysis_text)
        self.assertTrue(answer.analysis_version)

    def test_invalid_row_is_not_partially_written(self):
        result = self._import()
        self.assertFalse(
            result.survey.submissions.filter(answers__value="Invalid synthetic row").exists()
        )

    def test_second_run_is_idempotent(self):
        first = self._import()
        first_hashes = list(
            ImportedSubmissionSource.objects.order_by("source_record_key").values_list(
                "source_record_key", flat=True
            )
        )
        second = self._import()
        second_hashes = list(
            ImportedSubmissionSource.objects.order_by("source_record_key").values_list(
                "source_record_key", flat=True
            )
        )
        self.assertEqual(first.imported_count, 8)
        self.assertEqual(second.imported_count, 0)
        self.assertEqual(second.duplicate_count, 9)
        self.assertEqual(first_hashes, second_hashes)
        self.assertEqual(FeedbackSubmission.objects.count(), 8)
        self.assertEqual(AnalysisJob.objects.filter(survey=first.survey).count(), 1)

    def test_same_source_key_with_different_content_is_reported_not_overwritten(self):
        first = self._import()
        rows = FIXTURE_DATA.read_text(encoding="utf-8").splitlines()
        changed = json.loads(rows[0])
        changed["review"] = "Changed synthetic fixture content."
        rows[0] = json.dumps(changed)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "conflict.jsonl"
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            second = import_dataset(path, self.mapping, limit=50, seed=42, batch_size=3)

        self.assertEqual(second.imported_count, 0)
        self.assertEqual(second.conflict_count, 2)
        self.assertEqual(second.skip_reasons["deduplication_conflict"], 2)
        self.assertEqual(first.survey.submissions.count(), 8)

    def test_all_rows_mode_creates_one_survey_and_one_coalesced_analysis_job(self):
        result = import_dataset(
            FIXTURE_DATA,
            self.mapping,
            limit=None,
            seed=42,
            batch_size=3,
        )
        self.assertEqual(result.sampled_count, result.valid_count)
        self.assertEqual(result.imported_count, 8)
        self.assertEqual(result.batch.sampling_method, "all_valid_rows")
        self.assertIsNone(result.batch.requested_limit)
        self.assertIsNone(result.batch.random_seed)
        self.assertEqual(result.survey.submissions.count(), 8)
        self.assertEqual(AnalysisJob.objects.filter(survey=result.survey).count(), 1)

    def test_source_record_key_and_submission_relationship_are_unique(self):
        result = self._import()
        source = ImportedSubmissionSource.objects.first()
        other_submission = FeedbackSubmission.objects.create(survey=result.survey)
        with self.assertRaises(IntegrityError):
            with self.captureOnCommitCallbacks(execute=True):
                ImportedSubmissionSource.objects.create(
                    submission=other_submission,
                    batch=result.batch,
                    source_namespace=source.source_namespace,
                    source_record_key=source.source_record_key,
                    content_sha256="f" * 64,
                    source_version=result.batch.source_version,
                )

    def test_sensitive_source_values_are_not_persisted(self):
        self._import()
        persisted = []
        persisted.extend(Survey.objects.values_list("title", "description"))
        persisted.extend(FeedbackSubmission.objects.values_list("respondent_name", "respondent_email"))
        persisted.extend(Answer.objects.values_list("value", flat=True))
        persisted.extend(ImportedSubmissionSource.objects.values_list("source_item_id", "metadata"))
        persisted.extend(DatasetImportBatch.objects.values_list("summary", flat=True))
        self.assertNotIn("TEST-USER", repr(persisted))

    def test_statistics_can_read_imported_answers(self):
        result = self._import()
        payload = get_survey_pandas_stats(result.survey)
        titles = {chart["question"].title for chart in payload["charts"]}
        self.assertIn("Purchase amount", titles)
        self.assertIn("Overall rating", titles)

    def test_delete_submission_cascades_source_record(self):
        self._import()
        source = ImportedSubmissionSource.objects.first()
        source.submission.delete()
        self.assertFalse(ImportedSubmissionSource.objects.filter(pk=source.pk).exists())

    def test_unrelated_manual_submission_is_untouched(self):
        manual_survey = Survey.objects.create(title="Manual survey", slug="manual-survey")
        manual_submission = FeedbackSubmission.objects.create(survey=manual_survey)
        self._import()
        self.assertTrue(FeedbackSubmission.objects.filter(pk=manual_submission.pk).exists())
        self.assertFalse(
            ImportedSubmissionSource.objects.filter(submission=manual_submission).exists()
        )
