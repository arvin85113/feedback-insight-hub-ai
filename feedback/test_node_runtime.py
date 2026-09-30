import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from config.node_paths import NodePaths
from desktop_app.node_runtime import (
    WorkerSupervisor,
    bind_server,
    console_url,
    existing_console,
    setup_url,
    worker_command,
)


class FakeServer:
    def __init__(self, address, app, busy):
        self.address = address
        self._busy = busy

    def prepare(self):
        if self.address[1] in self._busy:
            raise OSError("address in use")


class BindServerTests(SimpleTestCase):
    def test_skips_busy_ports(self):
        server, port = bind_server(
            object(), server_factory=lambda address, app: FakeServer(address, app, {8750, 8751})
        )
        self.assertEqual(port, 8752)
        self.assertEqual(server.address, ("127.0.0.1", 8752))

    def test_gives_up_after_the_range(self):
        with self.assertRaises(RuntimeError):
            bind_server(
                object(),
                attempts=3,
                server_factory=lambda address, app: FakeServer(address, app, {8750, 8751, 8752}),
            )


class UrlTests(SimpleTestCase):
    def test_urls(self):
        base = console_url(8752)
        self.assertEqual(base, "http://127.0.0.1:8752/")
        self.assertEqual(setup_url(base, "a+b/c"), "http://127.0.0.1:8752/setup/?token=a%2Bb%2Fc")


class WorkerCommandTests(SimpleTestCase):
    def test_frozen_exe_reuses_itself(self):
        self.assertEqual(worker_command(executable="C:/app/FIH.exe", frozen=True), ["C:/app/FIH.exe", "--worker"])

    def test_source_run_uses_the_package(self):
        self.assertEqual(
            worker_command(executable="python.exe", frozen=False), ["python.exe", "-m", "desktop_app", "--worker"]
        )


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class WorkerSupervisorTests(SimpleTestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.state_file = Path(self._directory.name) / "worker.state"
        self.now = 0.0
        self.started = []

    def supervisor(self):
        def start():
            process = FakeProcess()
            self.started.append(process)
            return process

        return WorkerSupervisor(start, state_file=self.state_file, clock=lambda: self.now)

    def crash(self):
        self.started[-1].returncode = 1

    def test_restarts_three_times_then_stops(self):
        supervisor = self.supervisor()
        supervisor.start()
        self.assertEqual(self.state_file.read_text(encoding="utf-8"), "running")
        for _ in range(3):
            self.now += 10
            self.crash()
            supervisor.poll()
        self.assertEqual(len(self.started), 4)
        self.assertFalse(supervisor.stopped)
        self.now += 10
        self.crash()
        supervisor.poll()
        self.assertTrue(supervisor.stopped)
        self.assertEqual(len(self.started), 4)
        self.assertEqual(self.state_file.read_text(encoding="utf-8"), "stopped")

    def test_crashes_outside_the_window_do_not_accumulate(self):
        supervisor = self.supervisor()
        supervisor.start()
        for _ in range(6):
            self.now += 301
            self.crash()
            supervisor.poll()
        self.assertFalse(supervisor.stopped)
        self.assertEqual(len(self.started), 7)

    def test_healthy_worker_is_left_alone(self):
        supervisor = self.supervisor()
        supervisor.start()
        supervisor.poll()
        self.assertEqual(len(self.started), 1)

    def test_shutdown_terminates_and_clears_state(self):
        supervisor = self.supervisor()
        supervisor.start()
        supervisor.shutdown()
        self.assertTrue(self.started[-1].terminated)
        self.assertFalse(self.state_file.exists())


class ExistingConsoleTests(SimpleTestCase):
    def test_running_instance_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = NodePaths(Path(directory))
            paths.run_dir.mkdir(parents=True)
            paths.port_file.write_text("8753", encoding="utf-8")
            probed = []
            url = existing_console(paths, probe=lambda target: probed.append(target) or True)
            self.assertEqual(url, "http://127.0.0.1:8753/")
            self.assertEqual(probed, ["http://127.0.0.1:8753/healthz/"])

    def test_stale_port_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = NodePaths(Path(directory))
            self.assertIsNone(existing_console(paths, probe=lambda target: True))
            paths.run_dir.mkdir(parents=True)
            paths.port_file.write_text("8753", encoding="utf-8")
            self.assertIsNone(existing_console(paths, probe=lambda target: False))
            paths.port_file.write_text("garbage", encoding="utf-8")
            self.assertIsNone(existing_console(paths, probe=lambda target: True))


class WorkerProcessHelpersTests(SimpleTestCase):
    def test_command_output_goes_to_the_log_when_there_is_no_console(self):
        from desktop_app.node_runtime import LogStream

        with self.assertLogs("desktop_app.node.worker", level="INFO") as captured:
            stream = LogStream("desktop_app.node.worker")
            stream.write('{"status": "started"}\n')
            stream.flush()
        self.assertEqual(captured.records[0].getMessage(), '{"status": "started"}')

    def test_crash_is_logged_and_becomes_an_exit_code(self):
        from desktop_app.node_runtime import exit_code_of

        def boom():
            raise RuntimeError("worker failed")

        with self.assertLogs("desktop_app.node_runtime", level="ERROR") as captured:
            self.assertEqual(exit_code_of(boom), 1)
        self.assertIn("worker failed", captured.output[0] + str(captured.records[0].exc_info))
        self.assertEqual(exit_code_of(lambda: None), 0)
