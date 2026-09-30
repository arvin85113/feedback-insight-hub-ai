"""Filesystem layout of a local node installation (DEPLOYMENT_MODE=node).

Everything lives under one per-user root so backup and uninstall have a single
target.  Only this module decides the layout; settings, the launcher and the
console read paths from here.  Import-safe before Django is configured.
"""

import os
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

APP_DIR_NAME = "FeedbackInsightHub"


@dataclass(frozen=True)
class NodePaths:
    root: Path

    @classmethod
    def from_environment(cls, environ=None):
        environ = os.environ if environ is None else environ
        override = environ.get("FEEDBACK_HUB_NODE_HOME", "").strip()
        if override:
            return cls(Path(override).expanduser())
        local_app_data = environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return cls(Path(local_app_data) / APP_DIR_NAME)
        return cls(Path.home() / ".feedback-insight-hub")

    @property
    def data_dir(self):
        return self.root / "data"

    @property
    def database_file(self):
        return self.data_dir / "node.sqlite3"

    @property
    def artifacts_dir(self):
        return self.data_dir / "artifacts"

    @property
    def secrets_dir(self):
        return self.root / "secrets"

    @property
    def secret_key_file(self):
        return self.secrets_dir / "secret_key"

    @property
    def setup_dir(self):
        return self.root / "setup"

    @property
    def setup_token_file(self):
        return self.setup_dir / "token"

    @property
    def run_dir(self):
        return self.root / "run"

    @property
    def heartbeat_file(self):
        return self.run_dir / "worker.heartbeat"

    @property
    def worker_state_file(self):
        return self.run_dir / "worker.state"

    @property
    def port_file(self):
        return self.run_dir / "port"

    @property
    def logs_dir(self):
        return self.root / "logs"

    def ensure(self, restrict=None):
        """Create the layout; lock down private folders the first time they appear."""

        restrict = restrict_to_current_user if restrict is None else restrict
        for folder in (self.data_dir, self.artifacts_dir, self.run_dir, self.logs_dir):
            folder.mkdir(parents=True, exist_ok=True)
        for folder in (self.secrets_dir, self.setup_dir):
            if not folder.is_dir():
                folder.mkdir(parents=True)
                restrict(folder)


def restrict_to_current_user(path, *, platform=sys.platform, run=subprocess.run, environ=None):
    """Only the signed-in account may read the folder; files inside inherit it."""

    if platform == "win32":
        environ = os.environ if environ is None else environ
        user = environ["USERNAME"]
        run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"],
            check=True,
            capture_output=True,
        )
    else:
        os.chmod(path, 0o700)


def load_or_create_secret_key(path):
    path = Path(path)
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(50)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another process (launcher vs worker) created it first.
        return path.read_text(encoding="utf-8").strip()
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(key)
    return key


def issue_setup_token(paths):
    """A fresh one-time token; any earlier token stops working."""

    token = secrets.token_urlsafe(32)
    paths.setup_dir.mkdir(parents=True, exist_ok=True)
    paths.setup_token_file.write_text(token, encoding="utf-8")
    return token


def read_setup_token(paths):
    try:
        token = paths.setup_token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def clear_setup_token(paths):
    try:
        paths.setup_token_file.unlink()
    except FileNotFoundError:
        pass
