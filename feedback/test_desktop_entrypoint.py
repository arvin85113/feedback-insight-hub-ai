import logging
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from desktop_app.__main__ import _configure_file_logging, _external_env_candidates


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
