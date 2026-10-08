import os
import sys
import json
from pathlib import Path


def _configure_file_logging(*, local_app_data=None, log_dir=None, filename="desktop.log"):
    """Write warnings and crashes to a rotating log, replacing the console build.

    The windowed EXE has no console, so this file is the diagnostic channel.
    Log records never include credentials (see ``main``).  The launcher and the
    worker are separate processes, so each gets its own file to avoid two
    processes rotating the same log.
    """

    import logging
    from logging.handlers import RotatingFileHandler

    if log_dir is None:
        root = Path(local_app_data or os.getenv("LOCALAPPDATA", "").strip() or Path.cwd())
        log_dir = root / "FeedbackInsightHub" / "logs"
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / filename, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(handler)

    def log_uncaught(exc_type, exc, traceback):
        logging.getLogger("desktop_app").critical("uncaught exception", exc_info=(exc_type, exc, traceback))
        sys.__excepthook__(exc_type, exc, traceback)

    def log_thread_crash(args):
        # The console server and supervisor run in threads; without this their
        # crashes only reach a stderr that the windowed EXE does not have.
        logging.getLogger("desktop_app").critical(
            "uncaught exception in thread %s",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    import threading

    sys.excepthook = log_uncaught
    threading.excepthook = log_thread_crash
    return log_dir / filename


ROLE_FLAGS = (
    ("--worker", "worker"),
    ("--smoke-test", "smoke"),
)


def select_role(argv):
    for flag, role in ROLE_FLAGS:
        if flag in argv:
            return role
    return "launcher"


def parent_pid_from(argv):
    if "--parent-pid" not in argv:
        return None
    index = argv.index("--parent-pid") + 1
    try:
        return int(argv[index])
    except (IndexError, ValueError):
        return None


def prepare_environment(role, environ=None):
    """Every role is the local node: local data only, never the cloud .env."""

    environ = os.environ if environ is None else environ
    environ["DEPLOYMENT_MODE"] = "node"


def _run_smoke_test():
    import django

    django.setup()
    from django.conf import settings
    _configure_file_logging(log_dir=settings.NODE_PATHS.logs_dir, filename="smoke.log")
    import pystray  # noqa: F401 - bundled for the node launcher
    from cheroot import wsgi  # noqa: F401 - bundled for the node launcher
    from django.template.loader import get_template

    from feedback.background_analysis import PROFILE_PATH, pipeline_version

    for template in ("feedback/dashboard_base.html", "node/overview.html", "node/setup.html", "account/login.html",
                     "node/datasets.html", "node/jobs.html", "node/gemini_settings.html", "cloudsync/connection.html"):
        get_template(template)
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    pipeline_version(profile)
    # Smoke validates bundled code/templates only: no server, migration, DB or API call.
    from node import gemini  # noqa: F401


def main():
    role = select_role(sys.argv[1:])
    # Deployment credentials remain external to the bundle and are never logged.
    prepare_environment(role)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    if role == "smoke":
        _run_smoke_test()
        return
    from desktop_app import node_launcher

    # Each runner calls django.setup() and then configures its own log file.
    if role == "launcher":
        node_launcher.run_launcher()
        return
    from desktop_app.node_runtime import exit_code_of

    # A supervised child must exit on a crash so the launcher can restart it.
    parent_pid = parent_pid_from(sys.argv[1:])
    sys.exit(exit_code_of(lambda: node_launcher.run_worker(parent_pid=parent_pid)))


if __name__ == "__main__":
    main()
