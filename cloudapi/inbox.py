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
