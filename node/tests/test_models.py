from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from node.audit import LOGIN_FAILED, SETUP_COMPLETED, record
from node.models import AuditLogImmutable, NodeAuditEvent, NodeInstallation


class NodeInstallationTests(TestCase):
    def test_singleton_row_and_read_only_check(self):
        self.assertFalse(NodeInstallation.setup_complete())
        self.assertFalse(NodeInstallation.objects.exists())
        installation = NodeInstallation.load()
        self.assertEqual(installation.pk, 1)
        other = NodeInstallation()
        other.save()
        self.assertEqual(NodeInstallation.objects.count(), 1)


class AuditEventTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="o@example.com", email="o@example.com", password="x"
        )

    def test_record_captures_actor_ip_and_details(self):
        request = RequestFactory().get("/", REMOTE_ADDR="127.0.0.1")
        request.user = self.user
        event = record(SETUP_COMPLETED, request=request, target="Acme", step="done")
        self.assertEqual(event.actor, self.user)
        self.assertEqual(event.actor_email, "o@example.com")
        self.assertEqual(event.ip, "127.0.0.1")
        self.assertEqual(event.details, {"step": "done"})

    def test_events_cannot_be_changed_or_deleted(self):
        event = record(LOGIN_FAILED, target="x@example.com")
        event.target = "tampered"
        with self.assertRaises(AuditLogImmutable):
            event.save()
        with self.assertRaises(AuditLogImmutable):
            event.delete()
        with self.assertRaises(AuditLogImmutable):
            NodeAuditEvent.objects.all().update(target="tampered")
        with self.assertRaises(AuditLogImmutable):
            NodeAuditEvent.objects.all().delete()

    def test_deleting_the_user_keeps_the_event(self):
        event = record(SETUP_COMPLETED, actor=self.user)
        self.user.delete()
        event = NodeAuditEvent.objects.get(pk=event.pk)
        self.assertIsNone(event.actor)
        self.assertEqual(event.actor_email, "o@example.com")
