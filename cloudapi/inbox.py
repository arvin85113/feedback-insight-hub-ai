"""Cloud inbox for node-owned surveys (spec §2, §3, §6, §9).

Every intake runs under the survey row lock, in the order the spec fixes:
resend check first (so a reply that already succeeded stays a success even
after the survey changed), then the form version, then the atomic capacity
claim.  Nothing is written when any step refuses.
"""

from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.utils.crypto import salted_hmac

from feedback.models import Question, Survey

from .envelope import HASH_VERSION, answers_hash, build_envelope, canonical_bytes, payload_hash
from .models import InboxCounter, InboxSubmission, SubmissionReceipt


class InboxRejected(Exception):
    user_message = "問卷送出失敗"


class DefinitionOutdated(InboxRejected):
    user_message = "問卷已更新，請確認後重新送出"


class InboxFull(InboxRejected):
    user_message = "目前暫停收件，請稍後再試"


class ResendRejected(InboxRejected):
    user_message = "這份回覆無法重複送出，請重新填寫"


@dataclass
class AcceptResult:
    receipt: SubmissionReceipt
    reused: bool


def uses_inbox(survey):
    return bool(settings.CLOUD_INBOX_ENABLED and survey.owner_node_id and survey.inbox_since)


def respondent_ref(user):
    return salted_hmac("cloudapi.respondent", str(user.pk)).hexdigest() if user else ""


@transaction.atomic
def accept_submission(survey, *, user, submission_uuid, form_version, consent_follow_up, answers):
    survey = Survey.objects.select_for_update().get(pk=survey.pk)
    incoming_hash = payload_hash(
        survey_uuid=survey.uuid,
        definition_version=form_version,
        consent_follow_up=consent_follow_up,
        is_complete=True,
        voided_at=None,
        answers=answers,
    )

    existing = SubmissionReceipt.objects.filter(submission_uuid=submission_uuid).first()
    if existing is not None:
        same = (
            existing.survey_id == survey.pk
            and existing.user_id == getattr(user, "pk", None)
            and existing.payload_hash == incoming_hash
            and existing.status != SubmissionReceipt.Status.ABANDONED
        )
        if not same:
            raise ResendRejected()
        return AcceptResult(existing, reused=True)

    if form_version != survey.definition_version:
        raise DefinitionOutdated()

    now = timezone.now()
    sequence = survey.response_sequence + 1
    envelope = build_envelope(
        submission_uuid=submission_uuid,
        survey=survey,
        definition_version=form_version,
        response_sequence=sequence,
        submitted_at=now,
        consent_follow_up=consent_follow_up,
        respondent_ref=respondent_ref(user),
        name=user.get_full_name() if user else "",
        email=user.email if user else "",
        answers=answers,
    )
    size = len(canonical_bytes(envelope))
    if size > settings.CLOUD_INBOX_MAX_ITEM_BYTES:
        raise InboxFull()

    node = survey.owner_node
    InboxCounter.for_node(node)
    taken = InboxCounter.objects.filter(
        node=node,
        occupied_count__lte=settings.CLOUD_INBOX_MAX_COUNT - 1,
        occupied_bytes__lte=settings.CLOUD_INBOX_MAX_BYTES - size,
    ).update(occupied_count=F("occupied_count") + 1, occupied_bytes=F("occupied_bytes") + size)
    if not taken:
        raise InboxFull()

    survey.response_sequence = sequence
    survey.save(update_fields=["response_sequence"])
    Question.objects.filter(survey=survey, uuid__in=list(answers)).update(has_received_answer=True)
    InboxSubmission.objects.create(
        submission_uuid=submission_uuid,
        node=node,
        survey=survey,
        envelope=envelope,
        answers_hash=answers_hash(answers),
        payload_hash=incoming_hash,
        hash_version=HASH_VERSION,
        size_bytes=size,
    )
    receipt = SubmissionReceipt.objects.create(
        submission_uuid=submission_uuid,
        node=node,
        survey=survey,
        user=user,
        submitted_at=now,
        consent_follow_up=consent_follow_up,
        definition_version=form_version,
        response_sequence=sequence,
        payload_hash=incoming_hash,
    )
    return AcceptResult(receipt, reused=False)


QUARANTINE_REASONS = ("content_conflict", "definition_unavailable")


def ack_items(node, items):
    """Per-item conditional delete (spec §6); a batch reply never implies every item succeeded."""

    results = []
    for item in items:
        uid, hash_ = str(item["submission_uuid"]), str(item["payload_hash"])
        with transaction.atomic():
            pending = InboxSubmission.objects.filter(
                submission_uuid=uid, node=node, state=InboxSubmission.State.PENDING, payload_hash=hash_
            )
            sizes = list(pending.select_for_update().values_list("size_bytes", flat=True))
            deleted = pending.delete()[0] if sizes else 0
            if deleted:
                SubmissionReceipt.objects.filter(submission_uuid=uid, node=node).update(
                    status=SubmissionReceipt.Status.SYNCED, synced_at=timezone.now()
                )
                InboxCounter.objects.filter(node=node).update(
                    occupied_count=F("occupied_count") - 1, occupied_bytes=F("occupied_bytes") - sizes[0]
                )
                status = "acked"
            else:
                receipt = SubmissionReceipt.objects.filter(submission_uuid=uid, node=node).first()
                if receipt is None or receipt.status in (
                    SubmissionReceipt.Status.QUARANTINED,
                    SubmissionReceipt.Status.ABANDONED,
                ):
                    status = "not_found"
                elif receipt.status == SubmissionReceipt.Status.SYNCED and receipt.payload_hash == hash_:
                    status = "already_acked"
                else:
                    status = "conflict"  # never downgrade a synced receipt
        results.append({"submission_uuid": uid, "status": status})
    return results


def quarantine_items(node, items):
    for item in items:
        if item.get("reason") not in QUARANTINE_REASONS:
            raise ValueError("不支援的隔離原因")
    results = []
    for item in items:
        uid, reason = str(item["submission_uuid"]), item["reason"]
        with transaction.atomic():
            moved = InboxSubmission.objects.filter(
                submission_uuid=uid, node=node, state=InboxSubmission.State.PENDING
            ).update(state=InboxSubmission.State.QUARANTINED, quarantine_reason=reason)
            if moved:
                SubmissionReceipt.objects.filter(submission_uuid=uid, node=node).update(
                    status=SubmissionReceipt.Status.QUARANTINED,
                    quarantine_reason=reason,
                    resolution=SubmissionReceipt.Resolution.UNRESOLVED,
                )
        results.append({"submission_uuid": uid, "status": "quarantined" if moved else "not_found"})
    return results


def deadline_state(oldest_pending_at, now):
    if oldest_pending_at is None:
        return "ok"
    age_days = (now - oldest_pending_at).total_seconds() / 86400
    if age_days >= settings.CLOUD_INBOX_CRITICAL_DAYS:
        return "critical"
    if age_days >= settings.CLOUD_INBOX_WARN_DAYS:
        return "warn"
    return "ok"


def database_bytes():
    from django.db import connection

    if connection.vendor != "postgresql":
        return None
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_database_size(current_database())")
        return cursor.fetchone()[0]


def inbox_summary(node, now=None):
    now = now or timezone.now()
    items = InboxSubmission.objects.filter(node=node)
    pending = items.filter(state=InboxSubmission.State.PENDING)
    oldest = pending.order_by("received_at").values_list("received_at", flat=True).first()
    counter = InboxCounter.for_node(node)
    return {
        "pending_count": pending.count(),
        "quarantined_count": items.filter(state=InboxSubmission.State.QUARANTINED).count(),
        "occupied_count": counter.occupied_count,
        "occupied_bytes": counter.occupied_bytes,
        "max_count": settings.CLOUD_INBOX_MAX_COUNT,
        "max_bytes": settings.CLOUD_INBOX_MAX_BYTES,
        "oldest_pending_at": oldest.isoformat() if oldest else None,
        "deadline_state": deadline_state(oldest, now),
    }


def survey_sequences(node):
    abandoned = {}
    for survey_id, sequence in SubmissionReceipt.objects.filter(
        node=node, status=SubmissionReceipt.Status.ABANDONED
    ).values_list("survey_id", "response_sequence"):
        abandoned.setdefault(survey_id, []).append(sequence)
    return [
        {"survey_uuid": str(survey_uuid), "response_sequence": sequence,
         "abandoned_sequences": sorted(abandoned.get(pk, []))}
        for pk, survey_uuid, sequence in Survey.objects.filter(owner_node=node)
        .order_by("pk")
        .values_list("pk", "uuid", "response_sequence")
    ]
