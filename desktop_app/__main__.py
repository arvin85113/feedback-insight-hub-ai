import os
import sys
import json
from pathlib import Path


def _external_env_candidates(*, executable=None, local_app_data=None, explicit=None):
    """Return external settings locations from most to least explicit."""

    executable_path = Path(executable or sys.executable).resolve()
    executable_dir = executable_path.parent
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(executable_dir / ".env")
    if local_app_data:
        candidates.append(Path(local_app_data) / "FeedbackInsightHub" / ".env")

    # A development build under ``<repo>/dist/<name>`` may reuse the repo's
    # external .env.  Require repository markers so an arbitrary ancestor .env
    # is never loaded merely because the EXE was copied elsewhere.
    for parent in executable_dir.parents:
        if (parent / "manage.py").is_file() and (parent / "config" / "settings.py").is_file():
            candidates.append(parent / ".env")
            break

    seen = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in seen:
            seen.add(resolved)
            yield resolved


def _load_external_environment():
    if not getattr(sys, "frozen", False):
        return None

    from dotenv import load_dotenv

    candidates = _external_env_candidates(
        explicit=os.getenv("FEEDBACK_HUB_ENV_FILE", "").strip() or None,
        local_app_data=os.getenv("LOCALAPPDATA", "").strip() or None,
    )
    for candidate in candidates:
        if candidate.is_file():
            # Existing process environment always wins over an external file.
            load_dotenv(candidate, override=False)
            return candidate
    return None


def main():
    # Deployment credentials remain external to the bundle and are never logged.
    _load_external_environment()
    desktop_database_url = os.getenv("FEEDBACK_HUB_DATABASE_URL", "").strip()
    if desktop_database_url and not os.getenv("DATABASE_URL", "").strip():
        os.environ["DATABASE_URL"] = desktop_database_url
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    if "--smoke-test" in sys.argv:
        import dearpygui.dearpygui as dpg
        from desktop_app.app import FeedbackInsightDesktop
        from feedback.background_analysis import PROFILE_PATH, pipeline_version

        profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        pipeline_version(profile)
        dpg.create_context()
        try:
            application = FeedbackInsightDesktop()
            application._configure_style()
            application._build()
        finally:
            dpg.destroy_context()
        return
    from desktop_app.app import main as run_app

    run_app()


if __name__ == "__main__":
    main()
