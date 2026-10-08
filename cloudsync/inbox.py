"""Pull the cloud inbox into the node (spec §5 收件, §6).

Each reply is written in its own transaction together with its provenance and a
PendingAck; ACKs go out only after commit, and a PendingAck is removed only when
the cloud answers `acked` or `already_acked` for that item.  A crash anywhere in
between leaves either nothing or a row plus a PendingAck that the next cycle resends.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction

from cloudapi.envelope import ANSWERS_FORMAT, HASH_VERSION, answer_text, answers_hash, envelope_payload_hash
from feedback.analysis_jobs import suppress_analysis_scheduling
from feedback.models import Answer, FeedbackSubmission, Question, Survey

from .client import CloudError
from .definitions import upsert_definition
from .models import PendingAck, SyncedSubmissionSource, advance_and_schedule

logger = logging.getLogger(__name__)
PAGE_SIZE = 100
# The cloud ACKs one item per transaction (~0.4 s each on the free Render/Supabase tier);
# a batch must finish well inside the client timeout or it is resent forever.
ACK_BATCH = 10
WRITTEN = "written"
DUPLICATE = "duplicate"
CONTENT_CONFLICT = "content_conflict"
DEFINITION_UNAVAILABLE = "definition_unavailable"
ANSWERS_FORMAT_REASON = "answers_format"
ACK_DONE = ("acked", "already_acked")


@dataclass
class InboxResult:
    written: int = 0
    duplicates: int = 0
    quarantined: int = 0
    acked: int = 0
    problems: int = 0


def _ensure_definition(envelope, client):
    """Make sure the node has the definition the reply was answered against; False if it cannot."""

    survey_uuid, version = envelope["survey_uuid"], int(envelope["definition_version"])
    survey = Survey.objects.filter(uuid=survey_uuid).first()
    if survey is None or survey.definition_version < version:
        try:
            reply = client.get(f"surveys/{survey_uuid}/revisions/{version}/")
        except CloudError:
            return False
        upsert_definition(reply["definition"])
    known = set(
        str(value)
        for value in Question.objects.filter(survey__uuid=survey_uuid, uuid__in=list(envelope["answers"])).values_list(
            "uuid", flat=True
        )
    )
    return known == set(envelope["answers"])


def _parse_time(value):
    return datetime.fromisoformat(value) if value else None


def intake(envelope, client):
    uid = envelope["submission_uuid"]
    incoming = envelope_payload_hash(envelope)
    existing = FeedbackSubmission.objects.filter(idempotency_key=uid).select_related("synced_source").first()
    if existing is not None:
        source = getattr(existing, "synced_source", None)
        if source is None or source.payload_hash != incoming:
            return CONTENT_CONFLICT
        PendingAck.objects.get_or_create(submission_uuid=uid, defaults={"payload_hash": incoming})
        return DUPLICATE

    if envelope.get("answers_format") != ANSWERS_FORMAT:
        return ANSWERS_FORMAT_REASON
    if not _ensure_definition(envelope, client):
        return DEFINITION_UNAVAILABLE
    decoded = _decode_answers(envelope)
    if decoded is None:
        return ANSWERS_FORMAT_REASON

    respondent = envelope.get("respondent") or {}
    with transaction.atomic():
        survey = _write_reply(envelope, uid, incoming, respondent, decoded)
        # Outside the suppression, same transaction: a moved watermark queues analysis atomically.
        advance_and_schedule(survey)
    return WRITTEN


def _decode_answers(envelope):
    """{question uuid: (choice_codes, display value)}; None when a choice code is unknown (spec §7.3)."""

    questions = {str(q.uuid): q for q in Question.objects.filter(survey__uuid=envelope["survey_uuid"],
                                                               uuid__in=list(envelope["answers"]))}
    decoded = {}
    for key, value in envelope["answers"].items():
        question = questions[key]
        if question.kind not in ("single_choice", "multiple_choice"):
            decoded[key] = (None, answer_text(value))
            continue
        labels = {choice["code"]: choice["label"] for choice in question.choices}
        codes = value if isinstance(value, list) else [value]
        if not codes or any(code not in labels for code in codes):
            return None
        decoded[key] = (list(codes), ", ".join(labels[code] for code in codes))
    return decoded


def _write_reply(envelope, uid, incoming, respondent, decoded):
    with suppress_analysis_scheduling():
        survey = Survey.objects.select_for_update().get(uuid=envelope["survey_uuid"])
        submission = FeedbackSubmission.objects.create(
            survey=survey,
            user=None,
            idempotency_key=uid,
            submitted_at=_parse_time(envelope["submitted_at"]),
            consent_follow_up=envelope["consent_follow_up"],
            is_complete=envelope["is_complete"],
            voided_at=_parse_time(envelope["voided_at"]),
            respondent_ref=respondent.get("cloud_user_ref", ""),
            respondent_name=respondent.get("name", "")[:120],
            respondent_email=respondent.get("email", ""),
        )
        questions = {str(q.uuid): q for q in Question.objects.filter(survey=survey, uuid__in=list(envelope["answers"]))}
        Answer.objects.bulk_create(
            Answer(submission=submission, question=questions[key], value=decoded[key][1], choice_codes=decoded[key][0])
            for key in envelope["answers"]
        )
        SyncedSubmissionSource.objects.create(
            submission=submission,
            definition_version=envelope["definition_version"],
            definition_history=envelope["definition_history"],
            response_sequence=envelope["response_sequence"],
            answers_hash=answers_hash(envelope["answers"]),
            payload_hash=incoming,
            hash_version=HASH_VERSION,
            original_answers=envelope["answers"],
        )
        PendingAck.objects.create(submission_uuid=uid, payload_hash=incoming)
    return survey


def _send_acks(client, pending, result):
    """POST ACKs in batches; returns how many items the cloud confirmed."""

    confirmed = 0
    pending = list(pending)
    for start in range(0, len(pending), ACK_BATCH):
        batch = pending[start:start + ACK_BATCH]
        reply = client.post("inbox/ack/", {"items": [
            {"submission_uuid": str(item.submission_uuid), "payload_hash": item.payload_hash} for item in batch
        ]})
        statuses = {str(row["submission_uuid"]): row["status"] for row in reply.get("results", [])}
        for item in batch:
            status = statuses.get(str(item.submission_uuid), "")
            if status in ACK_DONE:
                item.delete()
                confirmed += 1
            else:
                PendingAck.objects.filter(pk=item.pk).update(last_status=status or "not_found")
                result.problems += 1
                logger.warning("inbox ack not confirmed: %s", status or "missing")
    result.acked += confirmed
    return confirmed


def sync_inbox(client):
    result = InboxResult()
    _send_acks(client, PendingAck.objects.order_by("created_at"), result)
    while True:
        page = client.get("inbox/", params={"limit": PAGE_SIZE})
        items = page.get("items", [])
        quarantine = []
        for envelope in items:
            outcome = intake(envelope, client)
            if outcome == WRITTEN:
                result.written += 1
            elif outcome == DUPLICATE:
                result.duplicates += 1
            else:
                quarantine.append({"submission_uuid": envelope["submission_uuid"], "reason": outcome})
        if quarantine:
            client.post("inbox/quarantine/", {"items": quarantine})
            result.quarantined += len(quarantine)
        uuids = [envelope["submission_uuid"] for envelope in items]
        confirmed = _send_acks(client, PendingAck.objects.filter(submission_uuid__in=uuids), result)
        # Stop when the cloud has nothing more, or when a page made no progress (every item still
        # pending in the cloud), so an unconfirmable item cannot make this loop spin.
        if not page.get("has_more") or not items or (confirmed == 0 and not quarantine):
            break
    return result
