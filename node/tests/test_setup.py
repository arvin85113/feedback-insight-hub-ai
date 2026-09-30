import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from config.node_paths import NodePaths, issue_setup_token, read_setup_token
from node.audit import SETUP_COMPLETED
from node.models import NodeAuditEvent, NodeInstallation
from organizations.models import Organization, OrganizationMembership

User = get_user_model()
VALID = {
    "organization_name": "Acme",
    "email": "Owner@Example.com",
    "password1": "Correct-Horse-9",
    "password2": "Correct-Horse-9",
}


class SetupTestCase(TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        self.paths = NodePaths(Path(self._home.name))
        self.paths.ensure(restrict=lambda _path: None)
        override = override_settings(NODE_PATHS=self.paths, NODE_SETUP_GATE=True)
        override.enable()
        self.addCleanup(override.disable)

    def approve(self):
        token = issue_setup_token(self.paths)
        response = self.client.get(f"/setup/?token={token}")
        self.assertRedirects(response, "/setup/", fetch_redirect_response=False)
        return token


class SetupGateTests(SetupTestCase):
    def test_pages_redirect_to_setup_until_done(self):
        self.assertRedirects(self.client.get("/dashboard/"), "/setup/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/node/"), "/setup/", fetch_redirect_response=False)

    def test_health_checks_are_not_gated(self):
        self.assertEqual(self.client.get("/healthz/").status_code, 200)


class SetupTokenTests(SetupTestCase):
    def test_missing_token_is_refused_without_echo(self):
        response = self.client.get("/setup/")
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "請從系統匣重新開啟設定", status_code=403)

    def test_wrong_token_is_refused_without_echo(self):
        issue_setup_token(self.paths)
        response = self.client.get("/setup/?token=guess-123")
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(response, "guess-123", status_code=403)

    def test_remote_address_is_refused_even_with_the_right_token(self):
        token = issue_setup_token(self.paths)
        response = self.client.get(f"/setup/?token={token}", REMOTE_ADDR="192.168.1.20")
        self.assertEqual(response.status_code, 403)

    def test_regenerated_token_invalidates_an_open_form(self):
        self.approve()
        issue_setup_token(self.paths)  # tray reopened setup
        response = self.client.post("/setup/", VALID)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.exists())


class SetupCompletionTests(SetupTestCase):
    def test_valid_form_creates_owner_organization_and_audit(self):
        self.approve()
        self.assertEqual(self.client.get("/setup/").status_code, 200)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post("/setup/", VALID)
        self.assertRedirects(response, reverse("node:overview"), fetch_redirect_response=False)

        owner = User.objects.get()
        self.assertEqual(owner.email, "owner@example.com")
        self.assertTrue(owner.is_manager)
        self.assertTrue(owner.check_password("Correct-Horse-9"))
        membership = OrganizationMembership.objects.get()
        self.assertEqual((membership.user, membership.role), (owner, "owner"))
        self.assertEqual(Organization.current().name, "Acme")
        self.assertTrue(NodeInstallation.setup_complete())
        self.assertTrue(NodeAuditEvent.objects.filter(action=SETUP_COMPLETED, actor=owner).exists())
        self.assertIsNone(read_setup_token(self.paths))
        self.assertEqual(self.client.get(reverse("node:overview")).status_code, 200)  # signed in

    def test_setup_is_gone_after_completion(self):
        token = self.approve()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post("/setup/", VALID)
        self.assertEqual(self.client.get("/setup/").status_code, 404)
        self.assertEqual(self.client.get(f"/setup/?token={token}").status_code, 404)
        self.assertEqual(self.client.post("/setup/", VALID).status_code, 404)
        self.assertEqual(User.objects.count(), 1)

    def test_mismatched_or_weak_password_creates_nothing(self):
        self.approve()
        response = self.client.post("/setup/", {**VALID, "password2": "Other-Horse-9"})
        self.assertEqual(response.status_code, 200)
        response = self.client.post("/setup/", {**VALID, "password1": "123", "password2": "123"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.exists())
        self.assertFalse(NodeInstallation.setup_complete())
