import hashlib
import json
import tempfile
from pathlib import Path
from unittest import TestCase

import duckdb
import requests

from feedback.importing.local_dataset import (
    LocalDatasetError,
    LockedFile,
    build_clean_layer,
    download_locked_file,
    file_sha256,
    load_source_lock,
    prepare_local_dataset,
)


class _InterruptedResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        yield b"partial"
        raise requests.ReadTimeout("fixture interruption")


class _InterruptedSession:
    def __init__(self):
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return _InterruptedResponse()


class _NoNetworkSession:
    def __init__(self):
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("cache hit must not use network")


def _write_parquet(path, rows):
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            """
            CREATE TABLE fixture (
              hotel_id INTEGER, user_id VARCHAR, title VARCHAR, text VARCHAR,
              overall INTEGER, cleanliness INTEGER, value INTEGER, location INTEGER,
              rooms INTEGER, sleep_quality INTEGER, post_date TIMESTAMP
            )
            """
        )
        connection.executemany("INSERT INTO fixture VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        escaped = str(path).replace("'", "''")
        connection.execute(f"COPY fixture TO '{escaped}' (FORMAT PARQUET)")
    finally:
        connection.close()


def _lock_payload(parquet_path, *, url_sha="a" * 40):
    return {
        "lock_version": 1,
        "dataset": "fixture/reviews",
        "source_revision": "b" * 40,
        "source_revision_date": "2024-01-01T00:00:00Z",
        "config": "default",
        "split": "train",
        "viewer_conversion_revision": url_sha,
        "source_card_field_evidence": {"hotel_id": "fixture identifier"},
        "files": [
            {
                "path": "default/train/0000.parquet",
                "size": parquet_path.stat().st_size,
                "lfs_sha256": file_sha256(parquet_path),
                "url": f"https://huggingface.co/datasets/fixture/reviews/resolve/{url_sha}/default/train/0000.parquet",
            }
        ],
        "cleaning_version": "fixture-clean-v1",
        "maximum_text_length": 50000,
        "required_columns": [
            "hotel_id", "user_id", "title", "text", "overall", "cleanliness",
            "value", "location", "rooms", "sleep_quality", "post_date"
        ],
    }


class LocalDatasetDownloadTests(TestCase):
    def test_interrupted_download_retries_once_and_never_publishes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            locked = LockedFile(
                "default/train/0000.parquet",
                100,
                hashlib.sha256(b"expected").hexdigest(),
                "https://huggingface.co/datasets/fixture/resolve/" + "a" * 40 + "/0000.parquet",
            )
            session = _InterruptedSession()
            with self.assertRaises(LocalDatasetError):
                download_locked_file(locked, root, session=session)
            self.assertEqual(session.calls, 2)
            self.assertFalse((root / locked.relative_path).exists())
            self.assertFalse((root / (locked.relative_path + ".part")).exists())

    def test_valid_cached_parquet_uses_no_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parquet = root / "source.parquet"
            _write_parquet(parquet, [(1, "u1", "t", "body", 5, 5, 5, 5, 5, 5, "2020-01-01")])
            locked = LockedFile("source.parquet", parquet.stat().st_size, file_sha256(parquet), "https://example.invalid")
            session = _NoNetworkSession()
            result = download_locked_file(locked, root, session=session)
            self.assertTrue(result.cache_hit)
            self.assertEqual(session.calls, 0)


class LocalDatasetCleaningTests(TestCase):
    def test_cleaning_reconciles_rows_and_reports_key_conflicts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "fixture.parquet"
            rows = [
                (1, "u1", "one", "unique", 5, 5, 5, 5, 5, 5, "2020-01-01"),
                (2, "u2", "same", "duplicate", 4, 4, 4, 4, 4, 4, "2020-01-02"),
                (2, "u2", "same", "duplicate", 4, 4, 4, 4, 4, 4, "2020-01-02"),
                (3, "u3", "a", "conflict-a", 3, 3, 3, 3, 3, 3, "2020-01-03"),
                (3, "u3", "b", "conflict-b", 3, 3, 3, 3, 3, 3, "2020-01-03"),
                (4, "u4", "bad", "missing overall", None, 2, 2, 2, 2, 2, "2020-01-04"),
                (5, "u5", "bad", None, 2, 2, 2, 2, 2, 2, "2020-01-05"),
                (6, "u6", None, "optional rating invalid", 2, 9, None, 2, 2, 2, "2020-01-06"),
            ]
            _write_parquet(source, rows)
            lock_path = root / "source.lock.json"
            lock_path.write_text(json.dumps(_lock_payload(source)), encoding="utf-8")
            source_lock = load_source_lock(lock_path)
            downloaded = type("Downloaded", (), {"path": source})()
            clean = root / "clean.parquet"
            report_path = root / "report.json"
            report = build_clean_layer(source_lock, (downloaded,), clean, report_path)

            self.assertEqual(report["input_rows"], 8)
            self.assertEqual(report["cleaning"]["retained_rows"], 3)
            self.assertEqual(report["candidate_key"]["exact_duplicate_rows_removed"], 1)
            self.assertEqual(report["candidate_key"]["conflicting_rows_excluded"], 2)
            self.assertEqual(report["cleaning"]["reconciliation"]["sum"], 8)
            clean_columns = {
                row[0]
                for row in duckdb.connect(":memory:").execute(
                    "DESCRIBE SELECT * FROM read_parquet(?)", [str(clean)]
                ).fetchall()
            }
            self.assertNotIn("user_id", clean_columns)
            cleanliness = duckdb.connect(":memory:").execute(
                "SELECT cleanliness FROM read_parquet(?) WHERE hotel_id = 6", [str(clean)]
            ).fetchone()[0]
            self.assertIsNone(cleanliness)

    def test_complete_manifest_is_reused_without_full_processing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "fixture-source.parquet"
            _write_parquet(source, [(1, "u1", "t", "body", 5, 5, 5, 5, 5, 5, "2020-01-01")])
            lock_path = root / "source.lock.json"
            payload = _lock_payload(source)
            lock_path.write_text(json.dumps(payload), encoding="utf-8")
            raw = root / "output" / "raw" / "default" / "train" / "0000.parquet"
            raw.parent.mkdir(parents=True)
            raw.write_bytes(source.read_bytes())
            first = prepare_local_dataset(lock_path, root / "output", session=_NoNetworkSession(), restrict_permissions=False)
            second_session = _NoNetworkSession()
            second = prepare_local_dataset(lock_path, root / "output", session=second_session, restrict_permissions=False)
            self.assertFalse(first.cache_hit)
            self.assertTrue(second.cache_hit)
            self.assertEqual(second_session.calls, 0)
