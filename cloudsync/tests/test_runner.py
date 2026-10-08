from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from cloudsync.client import TRANSIENT, UNAUTHORIZED, CloudError
from cloudsync.models import CloudLink
from cloudsync.runner import run_cycle
from cloudsync.tests.utils import memory_keyring
from cloudsync.tokens import save_token


class FakeClient:
    def __init__(self, error=None, heartbeat=None):
        self.error = error
        self.heartbeat = heartbeat or {}
        self.posts = []

    def post(self, path, body=None):
        if self.error:
            raise self.error
        self.posts.append(path)
        return self.heartbeat if path == "heartbeat/" else {}


class RunCycleTests(TestCase):
    def setUp(self):
        self._keyring = memory_keyring()
        self._keyring.__enter__()
        self.addCleanup(self._keyring.__exit__, None, None, None)
        CloudLink.relink("https://c", "88888888-8888-8888-8888-888888888888")
        save_token("https://c", "tok")

    def run_with(self, client, **kwargs):
        with patch("cloudsync.runner.client_for_link", return_value=client), \
             patch("cloudsync.runner.sync_definitions", return_value=0), \
             patch("cloudsync.runner.sync_inbox"):
            return run_cycle(**kwargs)

    def test_success_records_time_and_clears_errors(self):
        client = FakeClient()
        self.assertEqual(self.run_with(client), "ok")
        link = CloudLink.load()
        self.assertIsNotNone(link.last_success_at)
        self.assertEqual((link.consecutive_failures, link.last_error_kind), (0, ""))
        self.assertEqual(client.posts, ["heartbeat/"])

    def test_transient_failure_backs_off_and_waits(self):
        now = timezone.now()
        self.assertEqual(self.run_with(FakeClient(CloudError(TRANSIENT, retry_after=None)), now=now), TRANSIENT)
        link = CloudLink.load()
        self.assertEqual(link.consecutive_failures, 1)
        self.assertEqual(link.next_attempt_at, now + timedelta(seconds=60))
        self.assertEqual(self.run_with(FakeClient(), now=now + timedelta(seconds=10)), "waiting")
        self.assertEqual(self.run_with(FakeClient(), now=now + timedelta(seconds=10), force=True), "ok")

    def test_unauthorized_stops_until_forced(self):
        self.assertEqual(self.run_with(FakeClient(CloudError(UNAUTHORIZED))), UNAUTHORIZED)
        self.assertEqual(self.run_with(FakeClient()), "unauthorized")
        self.assertEqual(CloudLink.load().last_error_kind, UNAUTHORIZED)

    def test_relink_mid_cycle_does_not_record_old_results(self):
        def relink_then_succeed(client, link):
            CloudLink.relink("https://new", "ffffffff-ffff-ffff-ffff-ffffffffffff")
            return 0

        with patch("cloudsync.runner.client_for_link", return_value=FakeClient()), \
             patch("cloudsync.runner.sync_definitions", side_effect=relink_then_succeed), \
             patch("cloudsync.runner.sync_inbox"):
            run_cycle()
        fresh = CloudLink.load()
        self.assertEqual(fresh.api_url, "https://new")
        self.assertIsNone(fresh.last_success_at)  # the old run did not stamp the new link

    def test_not_linked(self):
        CloudLink.unlink()
        self.assertEqual(run_cycle(), "not_linked")

    def test_unbound_publication_cannot_report_complete_sync(self):
        from feedback.models import Survey
        from cloudsync.tests.test_results_local import published_state
        published_state(Survey.objects.create(title="Local fixture", slug="local-fixture"))
        self.assertEqual(self.run_with(FakeClient()), "results_incomplete")
        self.assertIsNotNone(CloudLink.load().last_success_at)  # connection itself succeeded

    def failed_upload(self, *, published_at=None, **survey_changes):
        from cloudsync.definitions import upsert_definition
        from cloudsync.models import ResultUpload
        from cloudsync.results import record_publication
        from cloudsync.tests.test_inbox import definition
        from cloudsync.tests.test_results_local import published_state
        from feedback.models import Survey

        survey, _ = upsert_definition(definition(1))
        upload = record_publication(published_state(survey))
        ResultUpload.objects.filter(pk=upload.pk).update(status="failed", last_error="invalid")
        if survey_changes:
            Survey.objects.filter(pk=survey.pk).update(**survey_changes)
        return survey, upload

    def test_failed_current_upload_cannot_report_complete_sync(self):
        self.failed_upload()
        self.assertEqual(self.run_with(FakeClient()), "results_incomplete")

    def test_failed_upload_of_a_superseded_version_does_not_keep_sync_incomplete(self):
        from cloudsync.models import ResultUpload
        from cloudsync.results import record_publication
        from cloudsync.tests.test_results_local import published_state

        survey, old = self.failed_upload()
        current = record_publication(published_state(survey))
        ResultUpload.objects.filter(pk=current.pk).update(status="uploaded")
        self.assertEqual(self.run_with(FakeClient()), "ok")
        self.assertEqual(ResultUpload.objects.get(pk=old.pk).status, "failed")  # history kept

    def test_failed_upload_of_an_archived_survey_does_not_keep_sync_incomplete(self):
        self.failed_upload(archived_at=timezone.now(), analysis_enabled=False)
        self.assertEqual(self.run_with(FakeClient()), "ok")

    def test_node_status_counts_only_current_failed_uploads(self):
        from cloudsync.models import ResultUpload
        from cloudsync.results import record_publication
        from cloudsync.tests.test_results_local import published_state
        from node.status import results_status

        survey, _ = self.failed_upload()
        self.assertEqual(results_status().state, "warn")
        ResultUpload.objects.filter(pk=record_publication(published_state(survey)).pk).update(status="uploaded")
        self.assertEqual(results_status().state, "ok")

class InboxCycleTests(TestCase):
    def setUp(self):
        self._keyring = memory_keyring()
        self._keyring.__enter__()
        self.addCleanup(self._keyring.__exit__, None, None, None)
        CloudLink.relink("https://c", "88888888-8888-8888-8888-888888888888")
        save_token("https://c", "tok")

    def test_cycle_runs_inbox_and_stores_heartbeat(self):
        heartbeat = {"node_uuid": "88888888-8888-8888-8888-888888888888",
                     "inbox": {"pending_count": 2, "deadline_state": "ok"}, "surveys": []}
        with patch("cloudsync.runner.client_for_link", return_value=FakeClient(heartbeat=heartbeat)), \
             patch("cloudsync.runner.sync_definitions", return_value=0), \
             patch("cloudsync.runner.sync_inbox") as sync_inbox:
            self.assertEqual(run_cycle(), "ok")
        sync_inbox.assert_called_once()
        self.assertEqual(CloudLink.load().inbox_status["pending_count"], 2)

    def test_abandoned_sequences_advance_the_watermark(self):
        from cloudsync.definitions import upsert_definition
        from cloudsync.inbox import intake
        from cloudsync.models import SurveySyncState
        from cloudsync.tests.test_inbox import FakeInboxClient, SURVEY_UUID, Q1, U1, U3, definition, envelope

        upsert_definition(definition(1))
        intake(envelope(1, U1, {Q1: "a"}), FakeInboxClient([]))
        intake(envelope(3, U3, {Q1: "c"}), FakeInboxClient([]))
        heartbeat = {"surveys": [{"survey_uuid": SURVEY_UUID, "response_sequence": 3, "abandoned_sequences": [2]}]}
        with patch("cloudsync.runner.client_for_link", return_value=FakeClient(heartbeat=heartbeat)), \
             patch("cloudsync.runner.sync_definitions", return_value=0), \
             patch("cloudsync.runner.sync_inbox"):
            run_cycle()
        self.assertEqual(SurveySyncState.objects.get(survey__uuid=SURVEY_UUID).synced_through_sequence, 3)


class ResultCycleTests(TestCase):
    def setUp(self):
        self._keyring = memory_keyring()
        self._keyring.__enter__()
        self.addCleanup(self._keyring.__exit__, None, None, None)
        CloudLink.relink("https://c", "88888888-8888-8888-8888-888888888888")
        save_token("https://c", "tok")

    def test_cycle_order_and_cloud_sequence_only_increases(self):
        from cloudsync.definitions import upsert_definition
        from cloudsync.models import SurveySyncState
        from cloudsync.tests.test_inbox import SURVEY_UUID, definition

        upsert_definition(definition(1))
        calls = []

        class OrderedClient(FakeClient):
            def post(self, path, body=None):
                calls.append(path)
                return super().post(path, body)

        for sequence in (7, 5):
            heartbeat = {"surveys": [{"survey_uuid": SURVEY_UUID, "abandoned_sequences": [], "publish_sequence": sequence}]}
            with patch("cloudsync.runner.client_for_link", return_value=OrderedClient(heartbeat=heartbeat)), \
                 patch("cloudsync.runner.sync_definitions", side_effect=lambda *a: calls.append("definitions") or 0), \
                 patch("cloudsync.runner.sync_inbox", side_effect=lambda *a: calls.append("inbox")), \
                 patch("cloudsync.runner.backfill_publications", side_effect=lambda: calls.append("backfill") or 0), \
                 patch("cloudsync.runner.upload_results", side_effect=lambda *a: calls.append("upload") or {}):
                self.assertEqual(run_cycle(force=True), "ok")
        self.assertEqual(calls[:5], ["definitions", "inbox", "heartbeat/", "backfill", "upload"])
        self.assertEqual(SurveySyncState.objects.get(survey__uuid=SURVEY_UUID).cloud_publish_sequence, 7)
