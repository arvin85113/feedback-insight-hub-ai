import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE_DIR = Path(__file__).resolve().parent.parent

DEPLOYMENT_MODE = os.getenv("DEPLOYMENT_MODE", "cloud").strip().lower() or "cloud"
if DEPLOYMENT_MODE not in {"cloud", "node"}:
    raise ImproperlyConfigured("DEPLOYMENT_MODE must be 'cloud' or 'node'.")
IS_NODE = DEPLOYMENT_MODE == "node"

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip() or "gemini-3.6-flash"
GEMINI_TIMEOUT_SECONDS = int(os.getenv("GEMINI_TIMEOUT_SECONDS", "45"))
GEMINI_THINKING_BUDGET = int(os.getenv("GEMINI_THINKING_BUDGET", "512"))
GEMINI_MAX_OUTPUT_TOKENS = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "4096"))
GEMINI_COMPACT_THINKING_BUDGET = int(os.getenv("GEMINI_COMPACT_THINKING_BUDGET", "256"))
GEMINI_COMPACT_MAX_OUTPUT_TOKENS = int(os.getenv("GEMINI_COMPACT_MAX_OUTPUT_TOKENS", "2048"))
# Gemini 3+ controls reasoning with levels (minimal/low/medium/high); budgets above
# only apply to gemini-2.x models.
GEMINI_THINKING_LEVEL = os.getenv("GEMINI_THINKING_LEVEL", "low").strip().lower() or "low"
GEMINI_COMPACT_THINKING_LEVEL = os.getenv("GEMINI_COMPACT_THINKING_LEVEL", "minimal").strip().lower() or "minimal"
_gemini_temperature = os.getenv("GEMINI_TEMPERATURE", "").strip()
GEMINI_TEMPERATURE = float(_gemini_temperature) if _gemini_temperature else None
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
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
_IS_RENDER_RUNTIME = bool(RENDER_EXTERNAL_HOSTNAME) or os.getenv("RENDER", "").lower() == "true"
ANALYSIS_AUTO_AI_ENABLED = os.getenv("ANALYSIS_AUTO_AI_ENABLED", "False").lower() == "true"

# Local development defaults to DEBUG; a deployed runtime must opt in explicitly.
DEBUG = os.getenv("DEBUG", "False" if (_IS_RENDER_RUNTIME or IS_NODE) else "True").lower() == "true"
if IS_NODE:
    from config.node_paths import NodePaths, load_or_create_secret_key

    NODE_PATHS = NodePaths.from_environment()
    NODE_PATHS.ensure()
    SECRET_KEY = load_or_create_secret_key(NODE_PATHS.secret_key_file)
else:
    SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "").strip()
    if not SECRET_KEY:
        if _IS_RENDER_RUNTIME or not DEBUG:
            raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DEBUG is off or on Render.")
        SECRET_KEY = "dev-secret-key-change-me"
ALLOWED_HOSTS = [host for host in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver,.onrender.com").split(",") if host]
if RENDER_EXTERNAL_HOSTNAME and RENDER_EXTERNAL_HOSTNAME not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(RENDER_EXTERNAL_HOSTNAME)
CSRF_TRUSTED_ORIGINS = [
    "https://feedback-insight-hub.onrender.com",
    "https://feedback-insight-hub-pa75.onrender.com",
]
if RENDER_EXTERNAL_HOSTNAME:
    render_origin = f"https://{RENDER_EXTERNAL_HOSTNAME}"
    if render_origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(render_origin)

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = os.getenv(
    "SECURE_SSL_REDIRECT",
    "True" if RENDER_EXTERNAL_HOSTNAME and not DEBUG else "False",
).lower() == "true"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
SECURE_HSTS_PRELOAD = SECURE_HSTS_SECONDS > 0

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
    "config.health.DatabaseUnavailableMiddleware",
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
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}
# Render services created outside the Blueprint may omit collectstatic. This
# keeps source static assets available while build.sh remains the preferred path.
WHITENOISE_USE_FINDERS = DEBUG or not (STATIC_ROOT / "css" / "app.css").is_file()

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

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard"},
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django": {"handlers": ["console"], "level": os.getenv("DJANGO_LOG_LEVEL", "INFO").upper(), "propagate": False},
        "feedback": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
}

if IS_NODE:
    # Local node: browsers on this machine only, plain HTTP on loopback.
    ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
    CSRF_TRUSTED_ORIGINS = []
    SECURE_SSL_REDIRECT = False
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False
    # Only NODE_DATABASE_URL: a developer .env points DATABASE_URL at Supabase.
    _node_database_url = os.getenv("NODE_DATABASE_URL", "").strip()
    DATABASES = {
        "default": dj_database_url.parse(_node_database_url, conn_max_age=600)
        if _node_database_url
        else {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": NODE_PATHS.database_file,
            "OPTIONS": {
                "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
                "transaction_mode": "IMMEDIATE",
                "timeout": 20,
            },
        }
    }
    INSTALLED_APPS += ["organizations", "node"]
    NODE_SETUP_GATE = True
