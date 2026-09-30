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

# Tests must not depend on the developer's .env: pin the model that fixtures use
# and never carry a real provider key into the test process.
GEMINI_MODEL = "gemini-2.5-flash"
GOOGLE_API_KEY = "test-key-not-a-real-credential"
LOGGING["root"]["level"] = "WARNING"  # noqa: F405
LOGGING["loggers"]["feedback"]["level"] = "WARNING"  # noqa: F405

# Existing suites exercise pages directly; node setup tests switch the gate back on.
NODE_SETUP_GATE = False
