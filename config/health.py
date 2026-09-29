"""Health checks and a graceful response while the database is unreachable.

The free Supabase plan pauses an idle project; until it resumes every page
that touches the ORM would fail with a 500.  These pieces keep that state
visible and harmless: no secrets, no database access in the fallback page.
"""

import logging

from django.db import DatabaseError, connection
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.views.decorators.cache import never_cache

logger = logging.getLogger(__name__)

DATABASE_RETRY_SECONDS = 60


@never_cache
def liveness(request):
    """Process is up; never touches the database (safe for platform probes)."""

    return JsonResponse({"ok": True})


@never_cache
def database_health(request):
    """Round-trip to the database; also counts as activity for idle-pausing hosts."""

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        logger.warning("database health check failed")
        return JsonResponse({"ok": False, "database": "unavailable"}, status=503)
    return JsonResponse({"ok": True, "database": "ok"})


class DatabaseUnavailableMiddleware:
    """Turn an unreachable database into a 503 page instead of a server error."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if not isinstance(exception, DatabaseError) or not _database_unreachable():
            return None
        logger.warning("database unavailable while serving %s", request.path)
        # Rendered without a request so no context processor queries the database.
        response = HttpResponse(render_to_string("db_unavailable.html"), status=503)
        response["Retry-After"] = str(DATABASE_RETRY_SECONDS)
        return response


def _database_unreachable():
    """Distinguish an outage from an ordinary query error such as an integrity violation."""

    try:
        connection.close()
        connection.ensure_connection()
    except DatabaseError:
        return True
    return False
