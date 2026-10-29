"""The cloud deletes its copy of a reply once the node ACKs it, so the ACK must never outrun durability.

Invariant: an ACK for a reply is sent only after the transaction that stored the reply has committed
and the database promises that commit survives power loss (SQLite synchronous FULL or stronger).
Application crashes are already safe; these tests pin the part a crash test cannot show.
"""

from django.db import connection
from django.test import TransactionTestCase

from cloudsync.client import CloudError
from cloudsync.definitions import upsert_definition
from cloudsync.inbox import NOT_DURABLE, commits_are_durable, sync_inbox
from cloudsync.models import PendingAck
from cloudsync.tests.test_inbox import Q1, U1, FakeInboxClient, definition, envelope
from feedback.models import FeedbackSubmission

SYNCHRONOUS_LEVELS = {"OFF": 0, "NORMAL": 1, "FULL": 2, "EXTRA": 3}


def set_synchronous(level):
    with connection.cursor() as cursor:
        cursor.execute(f"PRAGMA synchronous={level}")


def synchronous_level():
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA synchronous")
        return cursor.fetchone()[0]


class RecordingClient(FakeInboxClient):
    """Logs every ACK request into the same ordered list as the SQL the connection runs."""

    def __init__(self, events, pages):
        super().__init__(pages)
        self.events = events

    def post(self, path, body=None):
        if path == "inbox/ack/":
            self.events.append("ACK")
        return super().post(path, body)


class CommitsAreDurableTests(TransactionTestCase):
    def test_only_full_and_extra_count_as_durable(self):
        self.addCleanup(set_synchronous, "FULL")
        for name, level in SYNCHRONOUS_LEVELS.items():
            with self.subTest(synchronous=name):
                set_synchronous(name)
                self.assertEqual(synchronous_level(), level)
                self.assertEqual(commits_are_durable(), level >= 2)


class AckAfterDurableCommitTests(TransactionTestCase):
    """TransactionTestCase: a TestCase wraps the test in one transaction, so no commit would ever be observable."""

    def setUp(self):
        upsert_definition(definition(1))
        connection.ensure_connection()
        set_synchronous("FULL")
        self.addCleanup(set_synchronous, "FULL")
        self.addCleanup(connection.connection.set_trace_callback, None)

    def page(self, *envelopes):
        return {"items": list(envelopes), "has_more": False}

    def test_every_ack_follows_a_commit_of_the_reply_it_acknowledges(self):
        events = []

        def record(sql):
            sql = sql.strip().upper()
            if sql.startswith('INSERT INTO "CLOUDSYNC_PENDINGACK"'):
                events.append("STORED")
            elif sql == "COMMIT":
                events.append("COMMIT")

        connection.connection.set_trace_callback(record)
        client = RecordingClient(events, [self.page(envelope(1, U1, {Q1: "好"}))])

        result = sync_inbox(client)

        self.assertEqual(result.acked, 1)
        before_ack = events[: events.index("ACK")]
        self.assertEqual(before_ack[-2:], ["STORED", "COMMIT"], events)

    def test_ack_is_withheld_while_commits_could_roll_back_on_power_loss(self):
        set_synchronous("NORMAL")
        client = FakeInboxClient([self.page(envelope(1, U1, {Q1: "好"}))])

        with self.assertLogs("cloudsync.inbox", level="ERROR") as logs:
            result = sync_inbox(client)

        self.assertIn("not durable", logs.output[0])
        self.assertEqual([path for path, _ in client.posts], [])
        self.assertEqual((result.written, result.acked, result.problems), (1, 0, 1))
        self.assertTrue(FeedbackSubmission.objects.filter(idempotency_key=U1).exists())
        self.assertEqual(PendingAck.objects.get().last_status, NOT_DURABLE)

    def test_leftover_pending_acks_are_withheld_too(self):
        PendingAck.objects.create(submission_uuid=U1, payload_hash="h" * 64)
        set_synchronous("NORMAL")
        client = FakeInboxClient([self.page()])

        with self.assertLogs("cloudsync.inbox", level="ERROR"):
            sync_inbox(client)

        self.assertEqual(client.posts, [])
        self.assertEqual(PendingAck.objects.get().last_status, NOT_DURABLE)

    def test_withheld_ack_goes_out_on_the_next_cycle_once_durable(self):
        set_synchronous("NORMAL")
        with self.assertLogs("cloudsync.inbox", level="ERROR"):
            sync_inbox(FakeInboxClient([self.page(envelope(1, U1, {Q1: "好"}))]))
        set_synchronous("FULL")
        client = FakeInboxClient([self.page()])

        result = sync_inbox(client)

        self.assertEqual(result.acked, 1)
        self.assertEqual([path for path, _ in client.posts], ["inbox/ack/"])
        self.assertFalse(PendingAck.objects.exists())

    def test_unreachable_cloud_keeps_pending_ack_for_resend(self):
        client = FakeInboxClient([self.page(envelope(1, U1, {Q1: "好"}))], fail_ack_after=0)

        with self.assertRaises(CloudError):
            sync_inbox(client)

        self.assertEqual(PendingAck.objects.count(), 1)
        self.assertEqual(PendingAck.objects.get().last_status, "")
