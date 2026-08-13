import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE_DIR = Path(__file__).resolve().parent.parent

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
GEMINI_TIMEOUT_SECONDS = int(os.getenv("GEMINI_TIMEOUT_SECONDS", "45"))
GEMINI_THINKING_BUDGET = int(os.getenv("GEMINI_THINKING_BUDGET", "512"))
GEMINI_MAX_OUTPUT_TOKENS = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "4096"))
GEMINI_COMPACT_THINKING_BUDGET = int(os.getenv("GEMINI_COMPACT_THINKING_BUDGET", "256"))
GEMINI_COMPACT_MAX_OUTPUT_TOKENS = int(os.getenv("GEMINI_COMPACT_MAX_OUTPUT_TOKENS", "2048"))
AI_REPORT_MIN_RESPONSES = 3
AI_REPORT_FINGERPRINT_CHUNK_SIZE = 500
AI_REPORT_MAX_EVIDENCE_ITEMS = int(os.getenv("AI_REPORT_MAX_EVIDENCE_ITEMS", "40"))
AI_REPORT_MAX_ESTIMATED_INPUT_TOKENS = int(os.getenv("AI_REPORT_MAX_ESTIMATED_INPUT_TOKENS", "12000"))
AI_REPORT_COMPACT_MAX_EVIDENCE_ITEMS = int(os.getenv("AI_REPORT_COMPACT_MAX_EVIDENCE_ITEMS", "24"))
AI_REPORT_COMPACT_MAX_ESTIMATED_INPUT_TOKENS = int(
    os.getenv("AI_REPORT_COMPACT_MAX_ESTIMATED_INPUT_TOKENS", "6000")
)
AI_REPORT_RATE_LIMIT_BACKOFF_SECONDS = float(os.getenv("AI_REPORT_RATE_LIMIT_BACKOFF_SECONDS", "6"))
AI_REPORT_REQUEST_INTERVAL_SECONDS = float(os.getenv("AI_REPORT_REQUEST_INTERVAL_SECONDS", "6"))

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-secret-key-change-me")
DEBUG = os.getenv("DEBUG", "True").lower() == "true"
ALLOWED_HOSTS = [host for host in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver,.onrender.com").split(",") if host]
CSRF_TRUSTED_ORIGINS = [
    "https://feedback-insight-hub.onrender.com",
    "https://feedback-insight-hub-pa75.onrender.com",
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "feedback",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "feedback.context_processors.unread_notification_count",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": dj_database_url.config(default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}", conn_max_age=600)
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "zh-hant"
TIME_ZONE = "Asia/Taipei"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

_email_host = os.getenv("EMAIL_HOST", "").strip()
EMAIL_BACKEND = os.getenv(
    "EMAIL_BACKEND",
    "django.core.mail.backends.smtp.EmailBackend" if _email_host else "django.core.mail.backends.console.EmailBackend",
)
EMAIL_HOST = _email_host
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "True").lower() == "true"
EMAIL_USE_SSL = os.getenv("EMAIL_USE_SSL", "False").lower() == "true"
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "noreply@feedback-platform.local")

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "feedback:dashboard"
LOGOUT_REDIRECT_URL = "feedback:home"
AUTH_USER_MODEL = "accounts.User"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
