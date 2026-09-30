import os
import tempfile
from pathlib import Path
from unittest import skipIf

from django.test import SimpleTestCase

from config.node_paths import (
    NodePaths,
    clear_setup_token,
    issue_setup_token,
    load_or_create_secret_key,
    read_setup_token,
    restrict_to_current_user,
)


class NodePathsLocationTests(SimpleTestCase):
    def test_explicit_home_wins_over_local_app_data(self):
        paths = NodePaths.from_environment(
            {"FEEDBACK_HUB_NODE_HOME": "D:/node-home", "LOCALAPPDATA": "C:/Users/x/AppData/Local"}
        )
        self.assertEqual(paths.root, Path("D:/node-home"))

    def test_local_app_data_is_the_default_root(self):
        paths = NodePaths.from_environment({"LOCALAPPDATA": "C:/Users/x/AppData/Local"})
        self.assertEqual(paths.root, Path("C:/Users/x/AppData/Local") / "FeedbackInsightHub")

    def test_non_windows_falls_back_to_home(self):
        paths = NodePaths.from_environment({})
        self.assertEqual(paths.root, Path.home() / ".feedback-insight-hub")

    def test_layout_matches_spec(self):
        paths = NodePaths(Path("R"))
        self.assertEqual(paths.database_file, Path("R/data/node.sqlite3"))
        self.assertEqual(paths.artifacts_dir, Path("R/data/artifacts"))
        self.assertEqual(paths.secret_key_file, Path("R/secrets/secret_key"))
        self.assertEqual(paths.setup_token_file, Path("R/setup/token"))
        self.assertEqual(paths.heartbeat_file, Path("R/run/worker.heartbeat"))
        self.assertEqual(paths.worker_state_file, Path("R/run/worker.state"))
        self.assertEqual(paths.port_file, Path("R/run/port"))
        self.assertEqual(paths.logs_dir, Path("R/logs"))


class NodePathsEnsureTests(SimpleTestCase):
    def test_creates_directories_and_restricts_only_new_private_ones(self):
        restricted = []
        with tempfile.TemporaryDirectory() as directory:
            paths = NodePaths(Path(directory) / "home")
            paths.ensure(restrict=restricted.append)
            for folder in (
                paths.data_dir,
                paths.artifacts_dir,
                paths.secrets_dir,
                paths.setup_dir,
                paths.run_dir,
                paths.logs_dir,
            ):
                self.assertTrue(folder.is_dir(), folder)
            self.assertEqual(restricted, [paths.secrets_dir, paths.setup_dir])

            restricted.clear()
            paths.ensure(restrict=restricted.append)
            self.assertEqual(restricted, [])


class RestrictToCurrentUserTests(SimpleTestCase):
    def test_windows_removes_inheritance_and_grants_only_current_user(self):
        calls = []
        restrict_to_current_user(
            Path("C:/n/secrets"),
            platform="win32",
            run=lambda args, **kwargs: calls.append((args, kwargs)),
            environ={"USERNAME": "arvin"},
        )
        args, kwargs = calls[0]
        self.assertEqual(args[0], "icacls")
        self.assertIn("/inheritance:r", args)
        self.assertIn("arvin:(OI)(CI)F", args)
        self.assertTrue(kwargs["check"])

    @skipIf(os.name == "nt", "POSIX permission bits")
    def test_posix_limits_directory_to_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "secrets"
            target.mkdir()
            restrict_to_current_user(target, platform="linux")
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)


class SecretKeyTests(SimpleTestCase):
    def test_key_is_created_once_and_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "secret_key"
            first = load_or_create_secret_key(key_file)
            second = load_or_create_secret_key(key_file)
            self.assertEqual(first, second)
            self.assertGreaterEqual(len(first), 50)


class SetupTokenTests(SimpleTestCase):
    def test_issue_read_and_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = NodePaths(Path(directory))
            self.assertIsNone(read_setup_token(paths))
            first = issue_setup_token(paths)
            self.assertEqual(read_setup_token(paths), first)
            second = issue_setup_token(paths)
            self.assertNotEqual(first, second)
            self.assertEqual(read_setup_token(paths), second)
            clear_setup_token(paths)
            self.assertIsNone(read_setup_token(paths))
            clear_setup_token(paths)  # idempotent
