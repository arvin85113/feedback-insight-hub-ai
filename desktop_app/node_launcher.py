"""Local node roles: the tray launcher (web console + worker supervisor) and the worker."""

import logging
import os
import socket
import threading
import time
import webbrowser

logger = logging.getLogger("desktop_app.node")

SUPERVISE_INTERVAL_SECONDS = 5
STARTUP_WAIT_SECONDS = 60
CLOUD_SYNC_INTERVAL_SECONDS = 300


def run_worker(parent_pid=None):
    import django

    django.setup()
    from django.conf import settings
    from django.core.management import call_command

    from desktop_app.__main__ import _configure_file_logging
    from desktop_app.node_runtime import LogStream, watch_parent

    paths = settings.NODE_PATHS
    _configure_file_logging(log_dir=paths.logs_dir, filename="worker.log")
    if parent_pid is not None:
        # A killed launcher must not leave an unsupervised worker on the database.
        threading.Thread(target=watch_parent, args=(parent_pid,), name="parent-watch", daemon=True).start()
    call_command(
        "run_analysis_worker",
        worker_id=f"node-{socket.gethostname()}"[:64],
        output=str(paths.artifacts_dir),
        heartbeat_file=str(paths.heartbeat_file),
        stdout=LogStream("desktop_app.node.worker"),
        stderr=LogStream("desktop_app.node.worker"),
    )


def run_launcher():
    import django

    django.setup()
    from cheroot import wsgi
    from django.conf import settings
    from django.core.management import call_command
    from django.core.wsgi import get_wsgi_application
    from django.db import close_old_connections

    from config.node_paths import issue_setup_token
    from desktop_app import autostart
    from desktop_app.__main__ import _configure_file_logging
    from desktop_app.node_runtime import (
        WorkerSupervisor,
        acquire_instance_lock,
        bind_server,
        console_url,
        existing_console,
        run_periodically,
        setup_url,
        start_worker_process,
    )
    from desktop_app.node_tray import run_tray

    paths = settings.NODE_PATHS
    _configure_file_logging(log_dir=paths.logs_dir, filename="node.log")
    instance_lock = acquire_instance_lock(paths.run_dir / "launcher.lock")
    if instance_lock is None:
        # Another launcher owns this node (autostart plus a double-click, or an
        # impatient second click): wait for its console and show it instead.
        for _attempt in range(STARTUP_WAIT_SECONDS):
            running = existing_console(paths)
            if running:
                webbrowser.open(running)
                return
            time.sleep(1)
        logger.warning("another launcher holds the lock but its console did not answer")
        return

    # The node owns its local database; bring the schema up to date before serving.
    call_command("migrate", interactive=False, verbosity=0)
    server, port = bind_server(get_wsgi_application(), server_factory=wsgi.Server)
    paths.port_file.write_text(str(port), encoding="utf-8")
    base_url = console_url(port)
    logger.info("console listening at %s", base_url)

    supervisor = WorkerSupervisor(start_worker_process, state_file=paths.worker_state_file)
    supervisor.start()
    stop = threading.Event()

    def supervise():
        while not stop.wait(SUPERVISE_INTERVAL_SECONDS):
            supervisor.poll()

    def open_console():
        from node.models import NodeInstallation

        close_old_connections()
        if NodeInstallation.setup_complete():
            webbrowser.open(base_url)
        else:
            webbrowser.open(setup_url(base_url, issue_setup_token(paths)))

    def shutdown():
        stop.set()
        supervisor.shutdown()
        server.stop()
        try:
            paths.port_file.unlink()
        except FileNotFoundError:
            pass

    threading.Thread(target=server.serve, name="console-server", daemon=True).start()
    threading.Thread(target=supervise, name="worker-supervisor", daemon=True).start()

    def sync_cycle():
        from cloudsync.runner import run_cycle

        close_old_connections()
        run_cycle()

    threading.Thread(
        target=run_periodically, args=(stop, CLOUD_SYNC_INTERVAL_SECONDS, sync_cycle), name="cloud-sync", daemon=True
    ).start()
    open_console()
    try:
        run_tray(
            open_console=open_console,
            open_logs=lambda: os.startfile(paths.logs_dir),  # noqa: S606 - opens a folder in Explorer
            autostart_enabled=autostart.is_enabled,
            set_autostart=autostart.set_enabled,
            on_exit=shutdown,
        )
    finally:
        if not stop.is_set():
            shutdown()
