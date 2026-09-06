import os
import sys
from pathlib import Path


def main():
    if getattr(sys, "frozen", False):
        from dotenv import load_dotenv

        # Deployment credentials remain external to the bundle and are never logged.
        load_dotenv(Path(sys.executable).resolve().parent / ".env", override=False)
    desktop_database_url = os.getenv("FEEDBACK_HUB_DATABASE_URL", "").strip()
    if desktop_database_url and not os.getenv("DATABASE_URL", "").strip():
        os.environ["DATABASE_URL"] = desktop_database_url
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    if "--smoke-test" in sys.argv:
        import dearpygui.dearpygui as dpg
        from desktop_app.app import FeedbackInsightDesktop

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
