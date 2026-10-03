"""Run the cloud site in a subprocess with its own SQLite file (spec §13 端對端驗收)."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests
from django.conf import settings

STRIPPED = {"DEPLOYMENT_MODE", "FEEDBACK_HUB_NODE_HOME", "NODE_DATABASE_URL", "DJANGO_SETTINGS_MODULE", "DATABASE_URL"}


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class CloudServer:
    def __init__(self, workdir, *, startup_timeout=60):
        self.workdir = Path(workdir)
        self.startup_timeout = startup_timeout
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.env = {key: value for key, value in os.environ.items() if key not in STRIPPED}
        self.env.update({
            "DEPLOYMENT_MODE": "cloud",
            "DJANGO_SETTINGS_MODULE": "config.settings",
            "DATABASE_URL": f"sqlite:///{(self.workdir / 'cloud.sqlite3').as_posix()}",
            "DJANGO_SECRET_KEY": "e2e-cloud-secret-not-for-production",
            "DEBUG": "False",
            "ALLOWED_HOSTS": "127.0.0.1,localhost",
            "CLOUD_SYNC_PROTOTYPE_ENABLED": "True",
            "LOG_LEVEL": "WARNING",
            "PYTHONIOENCODING": "utf-8",
        })
        self.process = None
        self.token = None
        self.node_uuid = None

    def _manage(self, *args):
        result = subprocess.run(
            [sys.executable, "manage.py", *args], cwd=settings.BASE_DIR, env=self.env,
            capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"manage.py {args[0]} failed: {result.stderr[-2000:]}")
        return result.stdout

    def shell(self, code):
        return self._manage("shell", "-c", code)

    def __enter__(self):
        self._manage("migrate", "--noinput")
        self.token = self._manage("create_node_device", "--name", "e2e").strip().splitlines()[-1]
        self.node_uuid = self.shell(
            "from cloudapi.models import NodeDevice; print(NodeDevice.objects.get(name='e2e').uuid)"
        ).strip().splitlines()[-1]
        self.process = subprocess.Popen(
            [sys.executable, "manage.py", "runserver", "--noreload", f"127.0.0.1:{self.port}"],
            cwd=settings.BASE_DIR, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            try:
                if requests.get(f"{self.url}/healthz/", timeout=1).status_code == 200:
                    return self
            except requests.RequestException:
                time.sleep(0.5)
        self.__exit__(None, None, None)
        raise RuntimeError("cloud test server did not start")

    def __exit__(self, *exc):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        return False
