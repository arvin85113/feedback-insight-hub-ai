"""Local node launcher logic that needs neither a GUI nor a web server.

Kept free of pystray/cheroot imports so it is unit-testable on any OS.
"""

import logging
import subprocess
import sys
import time
import urllib.request
from collections import deque
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8750
PORT_ATTEMPTS = 20


def bind_server(app, *, server_factory, host=DEFAULT_HOST, start_port=DEFAULT_PORT, attempts=PORT_ATTEMPTS):
    last_error = None
    for port in range(start_port, start_port + attempts):
        server = server_factory((host, port), app)
        try:
            server.prepare()
        except OSError as error:
            last_error = error
            continue
        return server, port
    raise RuntimeError(f"{host}:{start_port}-{start_port + attempts - 1} 都已被占用") from last_error


def console_url(port, host=DEFAULT_HOST):
    return f"http://{host}:{port}/"


def setup_url(base_url, token):
    return f"{base_url}setup/?{urlencode({'token': token})}"


def worker_command(*, executable=None, frozen=None):
    executable = executable or sys.executable
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    return [executable, "--worker"] if frozen else [executable, "-m", "desktop_app", "--worker"]


def start_worker_process():
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(worker_command(), creationflags=flags)


class WorkerSupervisor:
    """Restart a crashed worker, but give up after repeated crashes in a short window."""

    def __init__(self, start_process, *, state_file, clock=time.monotonic, max_restarts=3, window_seconds=300):
        self._start_process = start_process
        self._state_file = state_file
        self._clock = clock
        self._max_restarts = max_restarts
        self._window_seconds = window_seconds
        self._crashes = deque()
        self.process = None
        self.stopped = False

    def _write_state(self, state):
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(state, encoding="utf-8")

    def start(self):
        self.stopped = False
        self._crashes.clear()
        self.process = self._start_process()
        self._write_state("running")

    def poll(self):
        if self.stopped or self.process is None:
            return
        code = self.process.poll()
        if code is None:
            return
        now = self._clock()
        self._crashes.append(now)
        while self._crashes and now - self._crashes[0] > self._window_seconds:
            self._crashes.popleft()
        if len(self._crashes) > self._max_restarts:
            self.stopped = True
            self._write_state("stopped")
            logger.error("worker exited with %s; restart limit reached, not restarting", code)
            return
        logger.warning("worker exited with %s; restarting", code)
        self.process = self._start_process()

    def shutdown(self, timeout=10):
        process, self.process = self.process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
        try:
            self._state_file.unlink()
        except FileNotFoundError:
            pass


class LogStream:
    """File-like sink for management command output in the windowed EXE (no stdout)."""

    def __init__(self, logger_name):
        self._logger = logging.getLogger(logger_name)

    def write(self, text):
        for line in text.splitlines():
            if line.strip():
                self._logger.info(line)

    def flush(self):
        pass

    def isatty(self):
        return False


def exit_code_of(run):
    """Run a child role; a crash becomes a logged exit code instead of a blocking error dialog."""

    try:
        run()
    except Exception:
        logger.exception("node process crashed")
        return 1
    return 0


def http_probe(url, timeout=1.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except OSError:
        return False


def existing_console(paths, *, probe=http_probe):
    """URL of a console another launcher already serves, or None."""

    try:
        port = int(paths.port_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    url = console_url(port)
    return url if probe(f"{url}healthz/") else None
