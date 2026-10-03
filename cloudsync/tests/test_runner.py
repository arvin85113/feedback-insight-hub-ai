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
    def __init__(self, error=None):
        self.error = error
        self.posts = []

    def post(self, path, body=None):
        if self.error:
            raise self.error
        self.posts.append(path)
        return {}


class RunCycleTests(TestCase):
    def setUp(self):
        self._keyring = memory_keyring()
        self._keyring.__enter__()
        self.addCleanup(self._keyring.__exit__, None, None, None)
        CloudLink.relink("https://c", "88888888-8888-8888-8888-888888888888")
        save_token("https://c", "tok")

    def run_with(self, client, **kwargs):
        with patch("cloudsync.runner.client_for_link", return_value=client), \
             patch("cloudsync.runner.sync_definitions", return_value=0):
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
             patch("cloudsync.runner.sync_definitions", side_effect=relink_then_succeed):
            run_cycle()
        fresh = CloudLink.load()
        self.assertEqual(fresh.api_url, "https://new")
        self.assertIsNone(fresh.last_success_at)  # the old run did not stamp the new link

    def test_not_linked(self):
        CloudLink.unlink()
        self.assertEqual(run_cycle(), "not_linked")
