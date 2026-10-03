import json
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from cloudsync.client import TRANSIENT, CloudClient, CloudError
from cloudsync.models import CloudLink, ResultUpload
from cloudsync.runner import run_cycle
from cloudsync.testing import CloudServer
from cloudsync.tests.test_e2e_inbox import SEED, SUBMIT
from cloudsync.tests.utils import memory_keyring
from cloudsync.tokens import save_token

CLOUD_PAYLOAD = """
import json
from feedback.models import Survey
from feedback.published_analysis import get_published_analysis_payload
p = get_published_analysis_payload(Survey.objects.get(uuid='{survey}'))
print(json.dumps({{"available": p["available"], "node": p.get("node_result"),
                  "title": (p.get("statistics") or {{}}).get("charts") is not None}}, default=str))
"""

CLOUD_RECORDS = """
from cloudapi.models import PublishedResultRecord
print(PublishedResultRecord.objects.filter(survey__uuid='{survey}').count())
"""

# An analysis setting change makes earlier node results stale (builder spec §7.2); a published title is frozen.
CLOUD_RENAME = """
from cloudapi.definition import serialize_definition, update_survey
from cloudapi.writes import change_definition
from feedback.models import Survey
s = Survey.objects.get(uuid='{survey}')
d = serialize_definition(s); update_survey(d, {{"analysis_enabled": False}})
change_definition(s.uuid, expected_version=s.definition_version, definition=d)
"""


class ResultEndToEndTests(TestCase):
    """One cloud subprocess (inbox enabled) shared by all tests; each test seeds its own survey."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Windows may keep the terminated server's SQLite handle briefly; leftover temp files are harmless.
        cls._workdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.cloud = CloudServer(Path(cls._workdir.name), inbox=True)
        cls.cloud.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.cloud.__exit__(None, None, None)
        cls._workdir.cleanup()
        super().tearDownClass()

    def setUp(self):
        self._keyring = memory_keyring()
        self._keyring.__enter__()
        self.addCleanup(self._keyring.__exit__, None, None, None)
        loopback = override_settings(CLOUD_SYNC_ALLOW_LOOPBACK_HTTP=True)
        loopback.enable()
        self.addCleanup(loopback.disable)
        CloudLink.relink(self.cloud.url, self.cloud.node_uuid)
        save_token(self.cloud.url, self.cloud.token)
        self.slug = self._testMethodName.replace("_", "-")[:40]
        self.survey_uuid = self.cloud.shell(SEED.replace("{slug}", self.slug)).strip().splitlines()[-1]
        self.output = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.output.cleanup)

    def submit(self, text="很好"):
        self.cloud.shell(SUBMIT.format(survey=self.survey_uuid, slug=self.slug, sub=uuid.uuid4(), text=text))

    def analyse(self):
        # The shared cloud also syncs earlier tests' surveys down, each with its own job;
        # the worker takes one job per run, so drain the queue.
        from feedback.models import AnalysisJob

        for _attempt in range(10):
            if not AnalysisJob.objects.filter(status="pending").exists():
                return
            call_command("run_analysis_worker_once", worker_id="e2e", output=self.output.name)
        self.fail("analysis queue did not drain")

    def cloud_payload(self):
        return json.loads(self.cloud.shell(CLOUD_PAYLOAD.format(survey=self.survey_uuid)).strip().splitlines()[-1])

    def cloud_records(self):
        return int(self.cloud.shell(CLOUD_RECORDS.format(survey=self.survey_uuid)).strip().splitlines()[-1])

    def publish_first_result(self, replies=2):
        for index in range(replies):
            self.submit(f"回覆{index}")
        self.assertEqual(run_cycle(force=True), "ok")
        self.analyse()
        # What a backup taken now (before the upload) would contain; settled uploads drop their content.
        self.backup = dict(
            ResultUpload.objects.filter(survey__uuid=self.survey_uuid).values_list("publish_sequence", "content")
        )
        self.assertEqual(run_cycle(force=True), "ok")

    def test_reply_analysis_upload_and_display(self):
        self.publish_first_result()
        payload = self.cloud_payload()
        self.assertTrue(payload["available"])
        node = payload["node"]
        self.assertTrue(node["is_latest"])
        self.assertEqual((node["coverage"]["analyzed_unique"], node["publish_sequence"]), (2, 1))

    def test_new_reply_and_definition_change_mark_the_result_old(self):
        self.publish_first_result()
        self.submit("新的回覆")
        run_cycle(force=True)
        node = self.cloud_payload()["node"]
        self.assertEqual((node["pending_new"], node["is_latest"], node["publish_sequence"]), (1, False, 1))
        self.cloud.shell(CLOUD_RENAME.format(survey=self.survey_uuid))
        self.assertFalse(self.cloud_payload()["node"]["definition_current"])

    def test_lost_upload_reply_is_resent_with_the_same_identity(self):
        for index in range(2):
            self.submit(f"回覆{index}")
        self.assertEqual(run_cycle(force=True), "ok")
        self.analyse()
        original = CloudClient.post_raw
        calls = {"n": 0}

        def lost_reply(client, path, data):
            reply = original(client, path, data)  # the cloud processed it...
            calls["n"] += 1
            if calls["n"] == 1:
                raise CloudError(TRANSIENT, "reply lost")  # ...but the node never heard back
            return reply

        with patch.object(CloudClient, "post_raw", lost_reply):
            self.assertEqual(run_cycle(force=True), TRANSIENT)
            self.assertEqual(ResultUpload.objects.get(survey__uuid=self.survey_uuid).status, "pending")
            self.assertEqual(run_cycle(force=True), "ok")
        self.assertEqual(ResultUpload.objects.get(survey__uuid=self.survey_uuid).status, "uploaded")
        self.assertEqual(self.cloud_records(), 1)

    def test_restored_node_resends_old_result_as_stale(self):
        self.publish_first_result()
        self.submit("第三筆")
        run_cycle(force=True)
        self.analyse()
        run_cycle(force=True)
        self.assertEqual(self.cloud_payload()["node"]["publish_sequence"], 2)
        # Restored from the backup above: sequence 1 is pending again with its original content.
        ResultUpload.objects.filter(survey__uuid=self.survey_uuid, publish_sequence=1).update(
            status="pending", content=self.backup[1]
        )
        run_cycle(force=True)
        self.assertEqual(ResultUpload.objects.get(survey__uuid=self.survey_uuid, publish_sequence=1).status, "stale")
        self.assertEqual(self.cloud_payload()["node"]["publish_sequence"], 2)
        self.assertEqual(self.cloud_records(), 2)
