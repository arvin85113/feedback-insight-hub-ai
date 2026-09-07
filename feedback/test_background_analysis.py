import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import duckdb
from django.test import SimpleTestCase, TestCase
from .analysis_input import AnalysisField
from .analysis_adapters import AnswerInput, TableInput, ParquetInput
from .background_analysis import (PROFILE_PATH, descriptors, calculate_statistics, calculate_text,
                                  LocalAnalysisCancelled, run_once, run_mock_stages, assemble_input)
from .models import Survey, Question, FeedbackSubmission, Answer
from .text_pipeline import estimate_sentiment_score
from .importing.local_dataset import file_sha256

FIELDS = (
    AnalysisField("overall", "ordinal", True, "整體評分", "scale", ("1", "2", "3", "4", "5")),
    AnalysisField("rooms", "ordinal", True, "客房評分", "scale", ("1", "2", "3", "4", "5")),
    AnalysisField("text", "text", True, "評論", "long_text", (), True),
)
ROWS = [dict(overall=i % 5 + 1, rooms=(i + 1) % 5 + 1 if i != 3 else None,
             text="clean comfortable room and friendly staff" if i % 2 else "the room was not good")
        for i in range(12)]


class AdapterParityTests(TestCase):
    def test_real_answer_and_table_adapters_share_statistics_text_and_inputs(self):
        survey = Survey.objects.create(title="Fixture", slug="fixture")
        questions = {Question.objects.create(survey=survey, title=f.title, kind=f.kind,
                    data_type=f.data_type, options_text="\n".join(f.options), order=i).pk: f
                     for i, f in enumerate(FIELDS)}
        for row in ROWS:
            submission = FeedbackSubmission.objects.create(survey=survey)
            Answer.objects.bulk_create([Answer(submission=submission, question_id=pk, value=str(row[f.name]))
                                        for pk, f in questions.items() if row[f.name] is not None])
        adapter = AnswerInput.from_answers(Answer.objects.all(), questions, version="fixture-v1", name="fixture")
        table = TableInput(ROWS, FIELDS, version="fixture-v1", name="fixture")
        profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        with self.assertNumQueries(0), patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
            q = descriptors(adapter)
            first, n = calculate_statistics(adapter, q)
            second, _ = calculate_statistics(table, q)
            self.assertEqual(first, second)
            text = calculate_text(adapter, profile)
            self.assertEqual(text, calculate_text(table, profile))
            self.assertEqual(first["inferential_analysis"][0]["valid_n"], 11)
            native = assemble_input(adapter, q, first, text, n, "v1")
            external = assemble_input(table, q, second, text, n, "v1")
            self.assertEqual(native["statistics"], external["statistics"])
            self.assertEqual(native["text_analysis"], external["text_analysis"])
            self.assertNotEqual(native["data_scope"]["source_kind"], external["data_scope"]["source_kind"])
        # Exercise actual ORM acquisition independently; it materializes a read-only version.
        loaded = AnswerInput.from_survey(survey, version="fixture-v1")
        self.assertEqual(len(list(loaded.scan(tuple(f.name for f in loaded.fields())))), 12)


class LocalPipelineTests(SimpleTestCase):
    def setUp(self):
        self.profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        self.adapter = TableInput(ROWS, FIELDS, version="fixture-v1", name="fixture")

    def test_unknown_is_not_neutral_and_negation_uses_existing_scoring(self):
        self.assertIsNone(estimate_sentiment_score("the purple object exists", profile=self.profile))
        self.assertLess(estimate_sentiment_score("not good", profile=self.profile), 0)
        self.assertIsNone(estimate_sentiment_score("goodness", profile=self.profile))
        table = TableInput([dict(text="purple object")], FIELDS, version="v1", name="fixture")
        payload = calculate_text(table, self.profile)
        self.assertEqual(payload["coverage"]["unknown_sentiment_documents"], 1)
        self.assertEqual(payload["category_sentiments"][0]["neutral"], 0)

    def test_original_chinese_sentiment_still_available(self):
        table = TableInput([dict(text="乾淨 舒適")], FIELDS, version="v1", name="fixture")
        self.assertGreater(calculate_text(table, self.profile)["summary"]["avg_sentiment_score"], 0)

    def test_redaction_does_not_change_english_language_detection(self):
        table = TableInput([dict(text="good clean room email test@example.com")], FIELDS, version="v1", name="fixture")
        payload = calculate_text(table, self.profile)
        self.assertEqual(payload["coverage"]["sentiment_documents"], 1)
        self.assertGreater(payload["summary"]["avg_sentiment_score"], 0)

    def test_review_length_uses_readable_bins_and_keeps_raw_summary(self):
        fields = (
            AnalysisField(
                "review_length", "discrete", False, "評論長度 Review length", "integer"
            ),
        )
        rows = [
            {"review_length": value}
            for value in (50, 99, 100, 249, 250, 499, 500, 999, 1000, 1999, 2000, 22387)
        ]
        adapter = TableInput(rows, fields, version="length-v1", name="fixture")

        payload, row_count = calculate_statistics(adapter, descriptors(adapter))

        chart = payload["charts"][0]
        self.assertEqual(row_count, 12)
        self.assertEqual(chart["min"], 50)
        self.assertEqual(chart["max"], 22387)
        self.assertEqual(chart["distribution_unit"], "字元")
        self.assertEqual(
            [item["value"] for item in chart["counts"]],
            ["少於 100", "100–249", "250–499", "500–999", "1,000–1,999", "2,000 以上"],
        )
        self.assertEqual([item["total"] for item in chart["counts"]], [2, 2, 2, 2, 2, 2])

    def test_local_publish_is_mock_private_and_cache_reuses_without_analysis(self):
        with tempfile.TemporaryDirectory() as directory, patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
            table = TableInput(ROWS + [dict(overall=1, rooms=1, text="ZeldaSecret email zelda@example.com clean room")],
                               FIELDS, version="privacy-fixture", name="fixture")
            path, hit = run_once(table, directory)
            self.assertFalse(hit)
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("ZeldaSecret", raw)
            self.assertNotIn("zelda@example.com", raw)
            result = json.loads(raw)["result"]
            self.assertEqual(result["ai_mode"], "mock")
            self.assertEqual(len(result["ai"]["stages"]), 3)
            self.assertTrue(result["ai"]["stages"]["statistics"]["output"]["categorical_distributions"] == [])
            with patch("feedback.background_analysis.calculate_statistics", side_effect=AssertionError("recalculation")):
                self.assertTrue(run_once(table, directory)[1])

    def test_invalid_mock_response_does_not_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                run_once(self.adapter, directory, provider=lambda *a: {"invalid": True})
            self.assertFalse(list(Path(directory).glob("*.json")))

    def test_cancelled_run_does_not_publish_partial_result(self):
        with tempfile.TemporaryDirectory() as directory:
            cancelled = False

            def progress(stage, _percent):
                nonlocal cancelled
                if stage == "產生測試模式 AI 結果":
                    cancelled = True

            with self.assertRaises(LocalAnalysisCancelled):
                run_once(
                    self.adapter,
                    directory,
                    cancel_requested=lambda: cancelled,
                    progress=progress,
                )
            self.assertFalse(list(Path(directory).glob("*.json")))
            self.assertFalse(list(Path(directory).glob("*.part")))
            self.assertFalse(list(Path(directory).glob("*.lock")))

    def test_changed_input_version_runs_again_and_preserves_previous(self):
        with tempfile.TemporaryDirectory() as directory:
            first, _ = run_once(self.adapter, directory)
            table = TableInput(ROWS, FIELDS, version="fixture-v2", name="fixture")
            second, hit = run_once(table, directory)
            self.assertFalse(hit)
            self.assertNotEqual(first, second)
            self.assertTrue(first.exists())

    def test_parquet_adapter_checks_manifest_and_projects_only_mapped_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "clean.parquet"
            with duckdb.connect(":memory:") as connection:
                connection.execute("CREATE TABLE t(overall INTEGER, rooms INTEGER, text VARCHAR)")
                connection.executemany("INSERT INTO t VALUES (?, ?, ?)", [(r["overall"], r["rooms"], r["text"]) for r in ROWS])
                connection.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
            (root / "manifest").mkdir()
            manifest = root / "manifest" / "dataset.json"
            manifest.write_text(json.dumps(dict(clean_path="clean.parquet", cleaning_version="v1",
                source=dict(dataset="fixture", source_revision="v1"), artifacts=[dict(path="clean.parquet",
                    size=path.stat().st_size, sha256=file_sha256(path))])), encoding="utf-8")
            mapping = root / "mapping.json"
            mapping.write_text(json.dumps(dict(dataset=dict(name="fixture", version="v1"), questions=[
                dict(source_field=f.name, data_type=f.data_type, required=False, title=f.title, kind=f.kind,
                    options=f.options, enable_keyword_tracking=f.tracked) for f in FIELDS])), encoding="utf-8")
            adapter = ParquetInput(manifest, mapping)
            self.assertEqual(list(adapter.scan(("overall", "rooms", "text"))), ROWS)
            with self.assertRaises(ValueError):
                list(adapter.scan(("user_id",)))
