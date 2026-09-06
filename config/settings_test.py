"""Explicitly isolated settings for local automated tests.

This module never reads or writes the configured website database.  PostgreSQL
concurrency tests require a separate, explicitly supplied test environment.
"""

from .settings import *  # noqa: F403


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
