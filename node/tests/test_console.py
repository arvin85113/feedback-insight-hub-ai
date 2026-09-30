import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from config.node_paths import NodePaths
from node.audit import ORGANIZATION_RENAMED, SETUP_COMPLETED, record
from node.models import NodeAuditEvent
from organizations.models import Organization, OrganizationMembership

User = get_user_model()
Role = OrganizationMembership.Role


class ConsoleTestCase(TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        self.paths = NodePaths(Path(self._home.name))
        self.paths.ensure(restrict=lambda _path: None)
        override = override_settings(NODE_PATHS=self.paths)
        override.enable()
        self.addCleanup(override.disable)
        self.organization = Organization.objects.create(name="Acme")
        self.owner = self._member("owner@example.com", Role.OWNER)

    def _member(self, email, role):
        user = User.objects.create_user(
            username=email, email=email, password="pw-Complex-123", role=User.Role.MANAGER
        )
        OrganizationMembership.objects.create(user=user, organization=self.organization, role=role)
        return user


class OverviewTests(ConsoleTestCase):
    def test_anonymous_is_sent_to_login(self):
        response = self.client.get(reverse("node:overview"))
        self.assertEqual(response.status_code, 302)

    def test_manager_outside_the_organization_is_forbidden(self):
        outsider = User.objects.create_user(username="m@example.com", password="x", role=User.Role.MANAGER)
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(reverse("node:overview")).status_code, 403)

    def test_owner_sees_every_status_and_recent_audit(self):
        record(SETUP_COMPLETED, actor=self.owner, target="Acme")
        self.client.force_login(self.owner)
        response = self.client.get(reverse("node:overview"))
        self.assertEqual(response.status_code, 200)
        for label in ("資料庫", "分析 Worker", "磁碟空間", "區域網路", "雲端連線", "未開放（僅限本機）", "未連線", "完成首次設定"):
            self.assertContains(response, label)

    def test_stopped_worker_is_listed_as_pending(self):
        self.paths.worker_state_file.write_text("stopped", encoding="utf-8")
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("node:overview")), "分析 Worker 需要處理")

    def test_admin_can_open_overview_and_settings(self):
        admin = self._member("admin@example.com", Role.ADMIN)
        self.client.force_login(admin)
        self.assertEqual(self.client.get(reverse("node:overview")).status_code, 200)
        self.assertEqual(self.client.get(reverse("node:settings")).status_code, 200)

    def test_console_nav_appears_on_existing_manager_pages(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("feedback:survey-manager"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("node:overview"))
        self.assertNotContains(response, "返回官網")

    def test_root_redirects_to_console(self):
        response = self.client.get("/")
        self.assertRedirects(response, reverse("node:overview"), fetch_redirect_response=False)


class SettingsTests(ConsoleTestCase):
    def test_rename_organization_is_audited(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("node:settings"), {"name": "Acme Taiwan"})
        self.assertRedirects(response, reverse("node:settings"), fetch_redirect_response=False)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.name, "Acme Taiwan")
        event = NodeAuditEvent.objects.get(action=ORGANIZATION_RENAMED)
        self.assertEqual(event.details, {"from": "Acme", "to": "Acme Taiwan"})

    def test_blank_name_is_rejected(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("node:settings"), {"name": "  "})
        self.assertEqual(response.status_code, 200)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.name, "Acme")
