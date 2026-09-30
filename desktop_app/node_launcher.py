"""Local node roles: the tray launcher (web console + worker supervisor) and the worker."""

import logging
import os
import socket
import threading
import webbrowser

logger = logging.getLogger("desktop_app.node")

SUPERVISE_INTERVAL_SECONDS = 5


def run_worker():
    import django

    django.setup()
    from django.conf import settings
    from django.core.management import call_command

    from desktop_app.__main__ import _configure_file_logging
    from desktop_app.node_runtime import LogStream

    paths = settings.NODE_PATHS
    _configure_file_logging(log_dir=paths.logs_dir, filename="worker.log")
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
        bind_server,
        console_url,
        existing_console,
        setup_url,
        start_worker_process,
    )
    from desktop_app.node_tray import run_tray

    paths = settings.NODE_PATHS
    _configure_file_logging(log_dir=paths.logs_dir, filename="node.log")
    running = existing_console(paths)
    if running:
        # A second double-click: show the running console instead of a second server.
        webbrowser.open(running)
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
