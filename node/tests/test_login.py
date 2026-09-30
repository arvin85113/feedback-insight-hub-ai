import tempfile
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from config.node_paths import NodePaths
from node.audit import LOGIN_FAILED, LOGIN_SUCCEEDED
from node.models import NodeAuditEvent
from node.setup import complete_setup

User = get_user_model()
PASSWORD = "Correct-Horse-9"


class NodeLoginTests(TestCase):
    def setUp(self):
        cache.clear()
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        paths = NodePaths(Path(self._home.name))
        paths.ensure(restrict=lambda _path: None)
        override = override_settings(NODE_PATHS=paths, NODE_SETUP_GATE=True)
        override.enable()
        self.addCleanup(override.disable)
        with self.captureOnCommitCallbacks(execute=True):
            self.owner = complete_setup(organization_name="Acme", email="owner@example.com", password=PASSWORD)

    def login(self, email, password):
        return self.client.post(reverse("account_login"), {"login": email, "password": password})

    def test_owner_signs_in_and_lands_on_console(self):
        response = self.login("owner@example.com", PASSWORD)
        self.assertRedirects(response, reverse("node:overview"), fetch_redirect_response=False)
        self.assertTrue(NodeAuditEvent.objects.filter(action=LOGIN_SUCCEEDED, actor=self.owner).exists())

    def test_email_case_does_not_matter(self):
        response = self.login("OWNER@Example.com", PASSWORD)
        self.assertRedirects(response, reverse("node:overview"), fetch_redirect_response=False)

    def test_wrong_password_is_audited(self):
        response = self.login("owner@example.com", "nope")
        self.assertEqual(response.status_code, 200)
        event = NodeAuditEvent.objects.get(action=LOGIN_FAILED)
        self.assertEqual(event.target, "owner@example.com")
        self.assertEqual(event.ip, "127.0.0.1")

    def test_repeated_failures_are_rate_limited(self):
        for _ in range(5):
            self.login("owner@example.com", "nope")
        response = self.login("owner@example.com", PASSWORD)
        self.assertNotEqual(response.status_code, 302)

    def test_legacy_login_url_forwards_to_allauth(self):
        response = self.client.get("/accounts/login/?next=/dashboard/")
        self.assertRedirects(response, "/auth/login/?next=/dashboard/", fetch_redirect_response=False)

    def test_anonymous_console_request_goes_to_allauth_login(self):
        response = self.client.get(reverse("node:overview"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/auth/login/"))

    def test_no_public_sign_up(self):
        self.assertEqual(self.client.get("/accounts/signup/").status_code, 404)
        self.client.post(
            "/auth/signup/",
            {"email": "new@example.com", "password1": PASSWORD, "password2": PASSWORD},
        )
        self.assertFalse(User.objects.filter(email="new@example.com").exists())

    def test_idle_session_expires(self):
        self.login("owner@example.com", PASSWORD)
        session = self.client.session
        session.set_expiry(timezone.now() - timedelta(seconds=1))
        session.save()
        response = self.client.get(reverse("node:overview"))
        self.assertEqual(response.status_code, 302)

    def test_idle_timeout_settings(self):
        self.assertEqual(settings.SESSION_COOKIE_AGE, 4 * 3600)
        self.assertTrue(settings.SESSION_SAVE_EVERY_REQUEST)
