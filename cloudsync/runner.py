"""One sync cycle; the launcher calls it every 5 minutes and the console's 「立即同步」 calls it with force=True."""

import logging
import threading
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .client import TRANSIENT, UNAUTHORIZED, CloudError, NotLinked, backoff_seconds, client_for_link
from .definitions import sync_definitions
from .inbox import sync_inbox
from .results import backfill_publications, upload_results
from .models import CloudLink, StaleLink, SurveySyncState, advance_and_schedule

logger = logging.getLogger(__name__)
_cycle_lock = threading.Lock()


def _apply_abandoned(heartbeat):
    from feedback.models import Survey

    for row in heartbeat.get("surveys", []):
        abandoned = row.get("abandoned_sequences") or []
        survey = Survey.objects.filter(uuid=row.get("survey_uuid")).first()
        if survey is None or not abandoned:
            continue
        with transaction.atomic():
            state, _ = SurveySyncState.objects.select_for_update().get_or_create(survey=survey)
            state.abandoned_sequences = sorted(
                set(state.abandoned_sequences) | {s for s in abandoned if s > state.synced_through_sequence}
            )
            state.save(update_fields=["abandoned_sequences"])
            advance_and_schedule(survey)


def _apply_publish_sequences(heartbeat):
    from feedback.models import Survey

    for row in heartbeat.get("surveys", []):
        sequence = row.get("publish_sequence")
        survey = Survey.objects.filter(uuid=row.get("survey_uuid")).first()
        if survey is None or not isinstance(sequence, int):
            continue
        with transaction.atomic():
            SurveySyncState.objects.get_or_create(survey=survey)
            state = SurveySyncState.objects.select_for_update().get(survey=survey)
            if sequence > state.cloud_publish_sequence:
                state.cloud_publish_sequence = sequence
                state.save(update_fields=["cloud_publish_sequence"])


def _record_success(link, now, heartbeat):
    CloudLink.update_if_current(
        link.generation,
        inbox_status=heartbeat.get("inbox") or {},
        last_success_at=now,
        last_error_kind="",
        last_error_message="",
        consecutive_failures=0,
        next_attempt_at=None,
    )


def _record_failure(link, error, now):
    failures = link.consecutive_failures + 1 if error.kind == TRANSIENT else link.consecutive_failures
    next_attempt = (
        now + timedelta(seconds=backoff_seconds(failures, error.retry_after)) if error.kind == TRANSIENT else None
    )
    CloudLink.update_if_current(
        link.generation,
        last_error_kind=error.kind,
        last_error_message=str(error)[:255],
        consecutive_failures=failures,
        next_attempt_at=next_attempt,
    )


def run_cycle(*, force=False, now=None):
    now = now or timezone.now()
    link = CloudLink.load()
    if not link.is_linked:
        return "not_linked"
    if not force:
        if link.last_error_kind == UNAUTHORIZED:
            return "unauthorized"
        if link.next_attempt_at and now < link.next_attempt_at:
            return "waiting"
    if not _cycle_lock.acquire(blocking=False):
        return "busy"
    try:
        client = client_for_link(link)
        sync_definitions(client, link)
        sync_inbox(client)
        # Heartbeat before numbering new uploads, so a restored node continues after the cloud.
        heartbeat = client.post("heartbeat/") or {}
        _apply_abandoned(heartbeat)
        _apply_publish_sequences(heartbeat)
        backfill_publications()
        uploads = upload_results(client)
    except NotLinked:
        return "not_linked"
    except StaleLink:
        # The link changed while syncing; the new link's next cycle starts cleanly.
        return "stale"
    except CloudError as error:
        logger.warning("cloud sync failed: %s", error.kind)
        _record_failure(link, error, now)
        return error.kind
    else:
        _record_success(link, now, heartbeat)
        from .models import ResultUpload
        from .publication_status import publication_issues
        if uploads.get("failed") or ResultUpload.objects.filter(status="failed").exists() or publication_issues().exists():
            # Connectivity succeeded, but the local-to-cloud workflow is incomplete.
            return "results_incomplete"
        return "ok"
    finally:
        _cycle_lock.release()
