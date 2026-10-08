import tempfile
from collections import namedtuple
from pathlib import Path

from django.test import SimpleTestCase, TestCase

from cloudsync.models import CloudLink
from config.node_paths import NodePaths
from feedback.worker_heartbeat import write_heartbeat
from node.status import (
    cloud_status,
    database_status,
    disk_status,
    inbox_status,
    lan_status,
    pending_items,
    worker_status,
)

Usage = namedtuple("Usage", "total used free")


class WorkerStatusTests(SimpleTestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.paths = NodePaths(Path(self._directory.name))
        self.paths.run_dir.mkdir(parents=True)

    def test_never_started(self):
        self.assertEqual(worker_status(self.paths, now=100).state, "off")

    def test_recent_idle_heartbeat_is_running(self):
        write_heartbeat(self.paths.heartbeat_file, "idle", clock=lambda: 100)
        item = worker_status(self.paths, now=130)
        self.assertEqual(item.state, "ok")
        self.assertIn("運作中", item.summary)

    def test_old_idle_heartbeat_is_unresponsive(self):
        write_heartbeat(self.paths.heartbeat_file, "idle", clock=lambda: 100)
        item = worker_status(self.paths, now=400)
        self.assertEqual(item.state, "warn")
        self.assertIn("無回應", item.summary)

    def test_busy_worker_is_not_reported_dead_during_a_long_job(self):
        write_heartbeat(self.paths.heartbeat_file, "busy", clock=lambda: 100)
        item = worker_status(self.paths, now=100 + 3600)
        self.assertEqual(item.state, "ok")
        self.assertIn("處理工作中", item.summary)

    def test_supervisor_gave_up(self):
        write_heartbeat(self.paths.heartbeat_file, "idle", clock=lambda: 100)
        self.paths.worker_state_file.write_text("stopped", encoding="utf-8")
        item = worker_status(self.paths, now=110)
        self.assertEqual(item.state, "warn")
        self.assertIn("已停止", item.summary)


class DiskAndFixedStatusTests(SimpleTestCase):
    def test_low_disk_warns(self):
        paths = NodePaths(Path("."))
        low = disk_status(paths, usage=lambda _path: Usage(100 * 1024**3, 99 * 1024**3, 1 * 1024**3))
        self.assertEqual(low.state, "warn")
        fine = disk_status(paths, usage=lambda _path: Usage(100 * 1024**3, 50 * 1024**3, 50 * 1024**3))
        self.assertEqual(fine.state, "ok")

    def test_lan_and_cloud_are_off_in_this_release(self):
        self.assertEqual(lan_status().summary, "未開放（僅限本機）")
        self.assertEqual(cloud_status(CloudLink()).summary, "未連線")

    def test_cloud_status_reflects_link(self):
        from datetime import timedelta

        from django.utils import timezone

        linked = CloudLink(api_url="https://c", node_uuid="99999999-9999-9999-9999-999999999999",
                           last_success_at=timezone.now() - timedelta(minutes=3))
        self.assertEqual(cloud_status(linked).state, "ok")
        failing = CloudLink(api_url="https://c", node_uuid="99999999-9999-9999-9999-999999999999",
                            last_error_kind="unauthorized", last_error_message="revoked")
        item = cloud_status(failing)
        self.assertEqual(item.state, "warn")
        self.assertIn("雲端連線已撤銷，請重新連結", item.summary)

    def test_pending_items_come_from_warnings(self):
        paths = NodePaths(Path("."))
        items = [disk_status(paths, usage=lambda _path: Usage(10, 10, 0)), lan_status()]
        self.assertEqual(len(pending_items(items)), 1)


class DatabaseStatusTests(TestCase):
    def test_reachable_database(self):
        self.assertEqual(database_status().state, "ok")


class InboxStatusTests(TestCase):
    def linked(self, **inbox):
        return CloudLink(api_url="https://c", node_uuid="99999999-9999-9999-9999-999999999999", inbox_status=inbox)

    def test_inbox_status_warns_on_deadline_and_ack_problems(self):
        self.assertEqual(inbox_status(CloudLink()).state, "off")
        self.assertEqual(inbox_status(self.linked(pending_count=4, deadline_state="warn")).state, "warn")
        ok = inbox_status(self.linked(pending_count=4, deadline_state="ok"))
        self.assertEqual((ok.state, ok.summary), ("ok", "待收 4 筆"))
        from cloudsync.models import PendingAck

        PendingAck.objects.create(submission_uuid="d1d1d1d1-d1d1-d1d1-d1d1-d1d1d1d1d1d1", payload_hash="h" * 64,
                                  last_status="conflict")
        self.assertEqual(inbox_status(self.linked(pending_count=0, deadline_state="ok")).state, "warn")


class ResultsStatusTests(TestCase):
    def test_results_status_counts_and_failures(self):
        from cloudsync.definitions import upsert_definition
        from cloudsync.models import ResultUpload
        from cloudsync.tests.test_inbox import definition
        from node.status import results_status

        linked = CloudLink(api_url="https://c", node_uuid="99999999-9999-9999-9999-999999999999")
        self.assertEqual(results_status(CloudLink()).state, "off")
        from cloudsync.tests.test_results_local import published_state

        survey, _ = upsert_definition(definition(1))
        state = published_state(survey)  # failures count only for the version the survey shows now
        ResultUpload.objects.create(survey=survey, publish_sequence=1, content_hash="h", content={},
                                    published_at=state.published_at)
        ok = results_status(linked)
        self.assertEqual(ok.state, "ok")
        self.assertIn("待上傳 1 份", ok.summary)
        ResultUpload.objects.update(status="failed", last_error="conflict")
        failing = results_status(linked)
        self.assertEqual((failing.state, failing.summary), ("warn", "1 份結果上傳失敗"))
