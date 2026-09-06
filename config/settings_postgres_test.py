"""Opt-in PostgreSQL settings for database-concurrency tests.

This module deliberately ignores ``DATABASE_URL``.  A caller must provide a
separate ``TEST_DATABASE_URL`` and acknowledge that it is isolated.  Django's
test runner will still create and destroy its own test database from this
connection, so the configured role needs test-database privileges.
"""

import os

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

from .settings import *  # noqa: F403


test_database_url = os.environ.get("TEST_DATABASE_URL", "").strip()
isolation_confirmed = os.environ.get("TEST_DATABASE_CONFIRM_ISOLATED", "").strip() == "1"
if not test_database_url:
    raise ImproperlyConfigured("PostgreSQL 併發測試必須明確設定 TEST_DATABASE_URL")
if not isolation_confirmed:
    raise ImproperlyConfigured(
        "PostgreSQL 併發測試必須設定 TEST_DATABASE_CONFIRM_ISOLATED=1，確認目標可建立及銷毀測試資料庫"
    )
if test_database_url == os.environ.get("DATABASE_URL", "").strip():
    raise ImproperlyConfigured("TEST_DATABASE_URL 不得與 DATABASE_URL 相同")

test_database = dj_database_url.parse(test_database_url, conn_max_age=0)
if test_database.get("ENGINE") != "django.db.backends.postgresql":
    raise ImproperlyConfigured("TEST_DATABASE_URL 必須使用 PostgreSQL")

database_name = str(test_database.get("NAME") or "").lower()
if not any(marker in database_name for marker in ("test", "isolated", "sandbox", "ci")):
    raise ImproperlyConfigured("隔離 PostgreSQL 的資料庫名稱必須包含 test、isolated、sandbox 或 ci")

DATABASES = {"default": test_database}
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
