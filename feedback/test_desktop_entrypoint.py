import logging
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from desktop_app.__main__ import (
    _configure_file_logging,
    parent_pid_from,
    prepare_environment,
    select_role,
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
        self.assertEqual(select_role(["--smoke-test"]), "smoke")

    def test_parent_pid_is_read_from_the_worker_arguments(self):
        self.assertEqual(parent_pid_from(["--worker", "--parent-pid", "4242"]), 4242)
        self.assertIsNone(parent_pid_from(["--worker"]))
        self.assertIsNone(parent_pid_from(["--worker", "--parent-pid", "nope"]))

    def test_every_role_runs_as_the_local_node(self):
        environ = {"DEPLOYMENT_MODE": "cloud"}
        for role in ("launcher", "worker", "smoke"):
            prepare_environment(role, environ)
            self.assertEqual(environ["DEPLOYMENT_MODE"], "node")

    def test_removed_workbench_flag_starts_the_node(self):
        self.assertEqual(select_role(["--legacy-workbench"]), "launcher")


class DesktopBuildScriptTests(unittest.TestCase):
    def test_every_project_app_bundles_its_migrations(self):
        # Django discovers migrations, URLconfs and management commands
        # dynamically, so PyInstaller misses them unless the build script
        # collects each project package; the node migrates on startup.
        root = Path(__file__).resolve().parent.parent
        script = (root / "scripts" / "build_desktop.ps1").read_text(encoding="utf-8-sig")
        apps = sorted(path.parent.parent.name for path in root.glob("*/migrations/__init__.py"))
        self.assertEqual(apps, ["accounts", "cloudapi", "cloudsync", "feedback", "node", "organizations"])
        for app in apps:
            self.assertIn(f"--collect-submodules {app} ", script)

    def test_configured_static_storage_backend_is_bundled(self):
        # STORAGES names the backend as a string, so PyInstaller cannot see it.
        root = Path(__file__).resolve().parent.parent
        script = (root / "scripts" / "build_desktop.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("--collect-submodules whitenoise ", script)

    def test_package_is_node_only(self):
        root = Path(__file__).resolve().parent.parent
        script = (root / "scripts" / "build_desktop.ps1").read_text(encoding="utf-8-sig")
        self.assertNotIn("dearpygui", script)
        self.assertIn("node_only_hook.py", script)
        self.assertFalse((root / "desktop_app" / "app.py").exists())


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
