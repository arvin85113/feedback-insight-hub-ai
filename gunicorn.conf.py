"""Gunicorn settings for the Render web service (auto-loaded from the project root).

The web tier only renders pages and reads published analysis results, so a small
worker pool with a bounded timeout fits the free instance's memory.
"""

import os

bind = f"0.0.0.0:{os.getenv('PORT', '10000')}"
workers = int(os.getenv("WEB_CONCURRENCY", "2"))
timeout = int(os.getenv("GUNICORN_TIMEOUT", "60"))
graceful_timeout = 30
# Load Django once in the master so workers share memory and fail fast on bad settings.
preload_app = True
# Recycle workers periodically to cap slow memory growth.
max_requests = 1000
max_requests_jitter = 100
accesslog = "-"
errorlog = "-"
