import csv
import tempfile
from pathlib import Path

import duckdb
from django.test import SimpleTestCase, TestCase

from feedback.importing.huggingface import prepare_parquet_sample
from feedback.importing.mapping import load_mapping
from feedback.importing.service import import_dataset
from feedback.models import Answer, FeedbackSubmission, ImportedSubmissionSource, Question


MAPPING_PATH = (
    Path(__file__).resolve().parent
    / "import_mappings"
    / "tripadvisor_hotel_reviews.json"
)
RATING_FIELDS = {
    "overall",
    "cleanliness",
    "value",
    "location",
    "rooms",
    "sleep_quality",
}


def _create_synthetic_parquet(path):
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            """
            CREATE TABLE reviews (
                hotel_id BIGINT,
                user_id VARCHAR,
                title VARCHAR,
                text VARCHAR,
                overall DOUBLE,
                cleanliness DOUBLE,
                value DOUBLE,
                location DOUBLE,
                rooms DOUBLE,
                sleep_quality DOUBLE,
                post_date TIMESTAMP
            )
            """
        )
        rows = []
        for index in range(12):
            rows.append(
                (
                    1000 + index % 3,
                    f"PRIVATE-USER-{index:02d}",
                    None if index == 1 else f"Synthetic title {index}",
                    f"Synthetic fixture review {index}",
                    float(index % 5 + 1),
                    None if index == 2 else float((index + 1) % 5 + 1),
                    float((index + 2) % 5 + 1),
                    float((index + 3) % 5 + 1),
                    float((index + 4) % 5 + 1),
                    float(index % 5 + 1),
                    f"2012-01-{index + 1:02d} 00:00:00",
                )
            )
        connection.executemany(
            "INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        escaped = path.as_posix().replace("'", "''")
        connection.execute(f"COPY reviews TO '{escaped}' (FORMAT PARQUET)")
    finally:
        connection.close()


class TripAdvisorMappingTests(SimpleTestCase):
    def test_mapping_uses_verified_schema_and_optional_aspects(self):
        mapping = load_mapping(MAPPING_PATH)
        questions = {question.source_field: question for question in mapping.questions}

        self.assertEqual(mapping.dataset.version, "1a1b7077c997eb496e402c6e9c97d91989eb24bb")
        self.assertFalse(mapping.survey.is_active)
        self.assertEqual(mapping.metadata_fields, ("hotel_id",))
        self.assertEqual(mapping.source_item_id_field, "")
        self.assertEqual(mapping.timestamp_field, "post_date")
        self.assertTrue(questions["overall"].required)
        for field_name in RATING_FIELDS:
            self.assertEqual(questions[field_name].kind, Question.Kind.SCALE)
            self.assertEqual(questions[field_name].data_type, Question.DataType.ORDINAL)
            self.assertEqual(questions[field_name].options, ("1", "2", "3", "4", "5"))
        for field_name in RATING_FIELDS - {"overall"}:
            self.assertFalse(questions[field_name].required)

    def test_preparation_never_outputs_raw_user_id_or_derived_source_text(self):
        preparation = load_mapping(MAPPING_PATH).huggingface_preparation

        self.assertNotIn("user_id", preparation.output_fields)
        self.assertIn("user_id", preparation.identity_hash_source_fields)
        self.assertNotIn("review", preparation.output_fields)
        self.assertNotIn("char", preparation.output_fields)
        self.assertNotIn("stay_year", preparation.output_fields)
        self.assertEqual(preparation.required_fields, ("overall", "text"))


class TripAdvisorSampleTests(SimpleTestCase):
    def test_seeded_reservoir_sample_is_reproducible_and_deidentified(self):
        mapping = load_mapping(MAPPING_PATH)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parquet_path = root / "synthetic.parquet"
            first_path = root / "first.csv"
            second_path = root / "second.csv"
            _create_synthetic_parquet(parquet_path)

            first = prepare_parquet_sample(mapping, [str(parquet_path)], first_path, limit=5, seed=42)
            second = prepare_parquet_sample(mapping, [str(parquet_path)], second_path, limit=5, seed=42)

            self.assertEqual(first.output_count, 5)
            self.assertEqual(first.output_sha256, second.output_sha256)
            self.assertEqual(first_path.read_bytes(), second_path.read_bytes())
            content = first_path.read_text(encoding="utf-8")
            self.assertNotIn("user_id", content.splitlines()[0])
            self.assertNotIn("PRIVATE-USER", content)
            self.assertIn("source_identity_sha256", content.splitlines()[0])


class TripAdvisorImportContractTests(TestCase):
    def test_optional_aspect_missing_count_idempotency_and_privacy(self):
        mapping = load_mapping(MAPPING_PATH)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parquet_path = root / "synthetic.parquet"
            sample_path = root / "sample.csv"
            _create_synthetic_parquet(parquet_path)
            prepare_parquet_sample(mapping, [str(parquet_path)], sample_path, limit=12, seed=42)

            first = import_dataset(sample_path, mapping, limit=25, seed=42, batch_size=4)
            second = import_dataset(sample_path, mapping, limit=25, seed=42, batch_size=4)

            self.assertEqual(first.imported_count, 12)
            self.assertEqual(second.imported_count, 0)
            self.assertEqual(first.missing_answer_counts["cleanliness"], 1)
            self.assertEqual(first.missing_answer_counts["title"], 1)
            self.assertEqual(FeedbackSubmission.objects.count(), 12)
            self.assertEqual(ImportedSubmissionSource.objects.count(), 12)
            self.assertEqual(
                Answer.objects.filter(question__title="清潔度 Cleanliness").count(),
                11,
            )
            persisted = list(Answer.objects.values_list("value", flat=True))
            persisted.extend(
                ImportedSubmissionSource.objects.values_list("metadata", flat=True)
            )
            self.assertNotIn("PRIVATE-USER", repr(persisted))
