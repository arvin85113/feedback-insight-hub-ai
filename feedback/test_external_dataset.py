import hashlib
import json
from pathlib import Path
import tempfile

from django.test import SimpleTestCase

from feedback.external_dataset import ExternalDatasetInvalid, validate_external_dataset


def dataset_fixture(root, marker="first"):
    """Tiny isolated Parquet fixture; never uses formal reviews or raw identifiers."""
    import duckdb

    (root / "clean").mkdir(exist_ok=True)
    (root / "manifest").mkdir(exist_ok=True)
    clean = root / "clean" / f"{marker}.parquet"
    with duckdb.connect(":memory:") as connection:
        connection.execute("CREATE TABLE fixture(overall INTEGER, text VARCHAR)")
        connection.executemany("INSERT INTO fixture VALUES (?, ?)",
                               [(5, "clean room"), (4, "friendly staff"), (2, "not good"), (3, "room")])
        connection.execute("COPY fixture TO ? (FORMAT PARQUET)", [str(clean)])
    manifest = root / "manifest" / f"{marker}.json"
    manifest.write_text(json.dumps({
        "source": {"dataset": "fixture/reviews", "source_revision": f"revision-{marker}"},
        "clean_path": f"clean/{marker}.parquet", "cleaning_version": "clean-v1",
        "counts": {"retained_rows": 4},
        "artifacts": [{"path": f"clean/{marker}.parquet", "size": clean.stat().st_size,
                       "sha256": hashlib.sha256(clean.read_bytes()).hexdigest()}],
    }), encoding="utf-8")
    mapping = root / f"mapping_{marker}.json"
    mapping.write_text(json.dumps({
        "mapping_version": "fixture-v1",
        "dataset": {"name": "fixture/reviews", "version": f"revision-{marker}"},
        "questions": [
            {"source_field": "overall", "title": "Overall", "kind": "scale", "data_type": "ordinal",
             "required": True, "options": ["1", "2", "3", "4", "5"]},
            {"source_field": "text", "title": "Comment", "kind": "long_text", "data_type": "text",
             "required": True, "enable_keyword_tracking": True},
        ],
    }), encoding="utf-8")
    return manifest, mapping, clean


class ExternalDatasetValidationTests(SimpleTestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.manifest, self.mapping, self.clean = dataset_fixture(self.root)

    def test_verified_metadata_is_bounded_and_deterministic(self):
        first = validate_external_dataset(self.manifest, self.mapping)
        self.assertEqual(first, validate_external_dataset(self.manifest, self.mapping))
        self.assertEqual(first.registration["row_count"], 4)
        self.assertEqual(first.registration["content_sha256"], hashlib.sha256(self.clean.read_bytes()).hexdigest())
        encoded = json.dumps(first.registration)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn("user_id", encoded)

    def test_modified_clean_file_is_rejected(self):
        self.clean.write_bytes(b"interrupted")
        with self.assertRaises(ExternalDatasetInvalid):
            validate_external_dataset(self.manifest, self.mapping)

    def test_path_escape_and_missing_fields_are_rejected(self):
        original = json.loads(self.manifest.read_text(encoding="utf-8"))
        for bad in ({**original, "clean_path": "../../outside.parquet"},
                    {**original, "counts": {"retained_rows": True}},
                    {**original, "artifacts": []}):
            self.manifest.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ExternalDatasetInvalid):
                validate_external_dataset(self.manifest, self.mapping)

    def test_mapping_revision_mismatch_is_rejected(self):
        body = json.loads(self.mapping.read_text(encoding="utf-8"))
        body["dataset"]["version"] = "other"
        self.mapping.write_text(json.dumps(body), encoding="utf-8")
        with self.assertRaises(ExternalDatasetInvalid):
            validate_external_dataset(self.manifest, self.mapping)
