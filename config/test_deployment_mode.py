import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase

from feedback.test_utils import cloud_only

PROBE = (
    "import json, django; from django.conf import settings; django.setup();"
    "db = settings.DATABASES['default'];"
    "print(json.dumps({'mode': settings.DEPLOYMENT_MODE, 'engine': db['ENGINE'], 'name': str(db['NAME']),"
    "'apps': settings.INSTALLED_APPS, 'hosts': settings.ALLOWED_HOSTS,"
    "'secret_from_file': settings.SECRET_KEY == (settings.NODE_PATHS.secret_key_file.read_text(encoding='utf-8').strip())}))"
)


def run_settings_probe(**overrides):
    env = {key: value for key, value in os.environ.items() if key not in {"NODE_DATABASE_URL", "DJANGO_SETTINGS_MODULE"}}
    env.update({"DJANGO_SETTINGS_MODULE": "config.settings", "PYTHONIOENCODING": "utf-8"}, **overrides)
    return subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=settings.BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


class NodeSettingsSubprocessTests(SimpleTestCase):
    def test_node_mode_uses_local_sqlite_even_when_database_url_is_set(self):
        with tempfile.TemporaryDirectory() as home:
            result = run_settings_probe(
                DEPLOYMENT_MODE="node",
                FEEDBACK_HUB_NODE_HOME=home,
                DATABASE_URL="postgres://must-not-be-used@example.invalid/db",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            probe = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(probe["mode"], "node")
            self.assertEqual(probe["engine"], "django.db.backends.sqlite3")
            self.assertEqual(Path(probe["name"]), Path(home) / "data" / "node.sqlite3")
            self.assertIn("node", probe["apps"])
            self.assertIn("organizations", probe["apps"])
            self.assertEqual(probe["hosts"], ["127.0.0.1", "localhost"])
            self.assertTrue(probe["secret_from_file"])

    def test_unknown_mode_refuses_to_start(self):
        result = run_settings_probe(DEPLOYMENT_MODE="hybrid")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DEPLOYMENT_MODE", result.stderr)


class CloudModeTests(SimpleTestCase):
    def test_current_process_mode_is_consistent(self):
        self.assertEqual(settings.IS_NODE, settings.DEPLOYMENT_MODE == "node")
        if not settings.IS_NODE:
            self.assertNotIn("node", settings.INSTALLED_APPS)
            self.assertNotIn("organizations", settings.INSTALLED_APPS)


@cloud_only
class CloudHidesNodeConsoleTests(TestCase):
    def test_node_urls_do_not_exist_on_the_cloud_site(self):
        self.assertEqual(self.client.get("/node/").status_code, 404)
        self.assertEqual(self.client.get("/setup/").status_code, 404)
