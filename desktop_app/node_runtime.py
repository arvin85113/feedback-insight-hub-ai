"""Local node launcher logic that needs neither a GUI nor a web server.

Kept free of pystray/cheroot imports so it is unit-testable on any OS.
"""

import logging
import os
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


def worker_command(*, executable=None, frozen=None, parent_pid=None):
    executable = executable or sys.executable
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    command = [executable, "--worker"] if frozen else [executable, "-m", "desktop_app", "--worker"]
    if parent_pid is not None:
        command += ["--parent-pid", str(parent_pid)]
    return command


def start_worker_process():
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(worker_command(parent_pid=os.getpid()), creationflags=flags)


def acquire_instance_lock(path):
    """Hold an OS lock for the launcher's lifetime; None when another launcher holds it.

    The OS releases the lock when the process dies, so a crash never leaves it stale.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")  # noqa: SIM115 - kept open for the process lifetime
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def process_alive(pid):
    if os.name == "nt":
        import ctypes

        synchronize, wait_timeout = 0x00100000, 0x00000102
        kernel32 = ctypes.windll.kernel32
        process = kernel32.OpenProcess(synchronize, False, int(pid))
        if not process:
            return False
        try:
            return kernel32.WaitForSingleObject(process, 0) == wait_timeout
        finally:
            kernel32.CloseHandle(process)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def watch_parent(parent_pid, *, alive=process_alive, on_orphan=None, wait=None, interval=5):
    """Stop the worker once the launcher that supervises it is gone.

    ``wait`` returns True to stop watching (e.g. ``threading.Event().wait``).
    """

    on_orphan = on_orphan or (lambda: os._exit(0))
    wait = wait or (lambda: time.sleep(interval) or False)
    while alive(parent_pid):
        if wait():
            return
    logger.warning("launcher %s is gone; worker exiting", parent_pid)
    on_orphan()


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


def run_periodically(stop, interval, func):
    """Call `func` every `interval` seconds until `stop` is set; one failure never ends the loop."""

    while not stop.is_set():
        try:
            func()
        except Exception:  # noqa: BLE001 - background loop must survive
            logger.exception("periodic task failed")
        if stop.wait(interval):
            return
