import logging
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from desktop_app.__main__ import (
    _configure_file_logging,
    _external_env_candidates,
    prepare_environment,
    select_role,
)


class DesktopEnvironmentDiscoveryTests(unittest.TestCase):
    def test_development_build_finds_repository_env_without_bundling_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "manage.py").touch()
            (root / "config").mkdir()
            (root / "config" / "settings.py").touch()
            executable = root / "dist" / "FeedbackInsightHub" / "FeedbackInsightHub.exe"
            executable.parent.mkdir(parents=True)

            candidates = list(
                _external_env_candidates(
                    executable=executable,
                    local_app_data=root / "user-data",
                )
            )

            self.assertIn((root / ".env").resolve(), candidates)
            self.assertLess(
                candidates.index((executable.parent / ".env").resolve()),
                candidates.index((root / ".env").resolve()),
            )

    def test_explicit_settings_file_has_highest_priority(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            explicit = root / "private" / "desktop.env"
            executable = root / "app" / "FeedbackInsightHub.exe"

            candidates = list(
                _external_env_candidates(
                    executable=executable,
                    local_app_data=root / "user-data",
                    explicit=explicit,
                )
            )

            self.assertEqual(candidates[0], explicit.resolve())
            self.assertIn(
                (root / "user-data" / "FeedbackInsightHub" / ".env").resolve(),
                candidates,
            )


class DesktopFileLoggingTests(unittest.TestCase):
    def test_warnings_and_uncaught_errors_are_written_to_local_log(self):
        root_logger = logging.getLogger()
        original_hook = sys.excepthook
        with tempfile.TemporaryDirectory() as temporary:
            log_path = _configure_file_logging(local_app_data=temporary)
            handler = root_logger.handlers[-1]
            try:
                logging.getLogger("desktop_app").warning("scan finished with warnings")
                try:
                    raise RuntimeError("boom")
                except RuntimeError:
                    with unittest.mock.patch.object(sys, "__excepthook__"):
                        sys.excepthook(*sys.exc_info())
                handler.flush()
                content = log_path.read_text(encoding="utf-8")
            finally:
                root_logger.removeHandler(handler)
                handler.close()
                sys.excepthook = original_hook
        self.assertEqual(log_path.name, "desktop.log")
        self.assertIn("scan finished with warnings", content)
        self.assertIn("uncaught exception", content)
        self.assertIn("RuntimeError: boom", content)

    def test_node_processes_log_to_their_own_file(self):
        root_logger = logging.getLogger()
        original_hook = sys.excepthook
        with tempfile.TemporaryDirectory() as temporary:
            log_path = _configure_file_logging(log_dir=Path(temporary) / "logs", filename="worker.log")
            handler = root_logger.handlers[-1]
            try:
                logging.getLogger("desktop_app").warning("probe")
                handler.flush()
                content = log_path.read_text(encoding="utf-8")
            finally:
                root_logger.removeHandler(handler)
                handler.close()
                sys.excepthook = original_hook
        self.assertEqual(log_path, Path(temporary) / "logs" / "worker.log")
        self.assertIn("probe", content)


class RoleSelectionTests(unittest.TestCase):
    def test_roles(self):
        self.assertEqual(select_role([]), "launcher")
        self.assertEqual(select_role(["--worker"]), "worker")
        self.assertEqual(select_role(["--legacy-workbench"]), "legacy")
        self.assertEqual(select_role(["--smoke-test"]), "smoke")

    def test_node_roles_never_load_the_external_cloud_env(self):
        environ = {}
        with unittest.mock.patch("desktop_app.__main__._load_external_environment") as load:
            prepare_environment("launcher", environ)
            prepare_environment("worker", environ)
        load.assert_not_called()
        self.assertEqual(environ["DEPLOYMENT_MODE"], "node")

    def test_legacy_workbench_stays_on_the_cloud_database(self):
        environ = {"FEEDBACK_HUB_DATABASE_URL": "postgres://example.invalid/db"}
        with unittest.mock.patch("desktop_app.__main__._load_external_environment", return_value=None):
            prepare_environment("legacy", environ)
        self.assertEqual(environ["DEPLOYMENT_MODE"], "cloud")
        self.assertEqual(environ["DATABASE_URL"], "postgres://example.invalid/db")


class DesktopBuildScriptTests(unittest.TestCase):
    def test_every_project_app_bundles_its_migrations(self):
        # Django discovers migrations, URLconfs and management commands
        # dynamically, so PyInstaller misses them unless the build script
        # collects each project package; the node migrates on startup.
        root = Path(__file__).resolve().parent.parent
        script = (root / "scripts" / "build_desktop.ps1").read_text(encoding="utf-8-sig")
        apps = sorted(path.parent.parent.name for path in root.glob("*/migrations/__init__.py"))
        self.assertEqual(apps, ["accounts", "feedback", "node", "organizations"])
        for app in apps:
            self.assertIn(f"--collect-submodules {app} ", script)

    def test_configured_static_storage_backend_is_bundled(self):
        # STORAGES names the backend as a string, so PyInstaller cannot see it.
        root = Path(__file__).resolve().parent.parent
        script = (root / "scripts" / "build_desktop.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("--collect-submodules whitenoise ", script)


class DesktopThreadLoggingTests(unittest.TestCase):
    def test_crash_in_a_background_thread_reaches_the_log(self):
        import threading

        root_logger = logging.getLogger()
        original_hook, original_thread_hook = sys.excepthook, threading.excepthook
        with tempfile.TemporaryDirectory() as temporary:
            log_path = _configure_file_logging(log_dir=Path(temporary), filename="node.log")
            handler = root_logger.handlers[-1]
            try:
                worker = threading.Thread(target=lambda: 1 / 0, name="console-server")
                worker.start()
                worker.join()
                handler.flush()
                content = log_path.read_text(encoding="utf-8")
            finally:
                root_logger.removeHandler(handler)
                handler.close()
                sys.excepthook, threading.excepthook = original_hook, original_thread_hook
        self.assertIn("console-server", content)
        self.assertIn("ZeroDivisionError", content)
