import tempfile
import unittest
from pathlib import Path

from desktop_app.__main__ import _external_env_candidates


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
