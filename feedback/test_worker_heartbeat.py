import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from feedback.worker_heartbeat import read_heartbeat, write_heartbeat


class WorkerHeartbeatTests(SimpleTestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run" / "worker.heartbeat"
            write_heartbeat(path, "busy", clock=lambda: 1000.5, pid=42)
            self.assertEqual(read_heartbeat(path), {"at": 1000.5, "state": "busy", "pid": 42})

    def test_missing_or_corrupt_file_reads_as_none(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.heartbeat"
            self.assertIsNone(read_heartbeat(path))
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(read_heartbeat(path))
            path.write_text('{"state": "idle"}', encoding="utf-8")
            self.assertIsNone(read_heartbeat(path))
