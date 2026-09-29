from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase
from django.urls import reverse


class HealthCheckTests(TestCase):
    def test_liveness_never_needs_the_database(self):
        with patch("django.db.backends.base.base.BaseDatabaseWrapper.cursor", side_effect=OperationalError):
            response = self.client.get(reverse("healthz"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})

    def test_database_health_reports_reachability_without_details(self):
        self.assertEqual(self.client.get(reverse("healthz-db")).json(), {"ok": True, "database": "ok"})
        with patch("config.health.connection.cursor", side_effect=OperationalError("password=secret")):
            response = self.client.get(reverse("healthz-db"))
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret", response.content.decode())


class DatabaseUnavailableMiddlewareTests(TestCase):
    def test_unreachable_database_renders_503_page(self):
        with patch("feedback.views.HomeView.get", side_effect=OperationalError("connection refused")), patch(
            "config.health._database_unreachable", return_value=True
        ):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Retry-After"], "60")
        self.assertContains(response, "資料庫正在喚醒中", status_code=503)

    def test_ordinary_query_errors_still_raise(self):
        with patch("feedback.views.HomeView.get", side_effect=OperationalError("bad query")), patch(
            "config.health._database_unreachable", return_value=False
        ):
            with self.assertRaises(OperationalError):
                self.client.get("/")
