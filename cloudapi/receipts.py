"""Reads that combine inbox receipts with existing FeedbackSubmission rows (spec §1 數量定義, §3).

A reply is counted once by its uuid: receipts reuse FeedbackSubmission.idempotency_key,
and abandoned receipts are not counts.
"""

from django.db.models import Count

from feedback.local_service import format_payload_date, format_payload_datetime
from feedback.models import FeedbackSubmission

from .models import SubmissionReceipt


def _live_receipts():
    return SubmissionReceipt.objects.exclude(status=SubmissionReceipt.Status.ABANDONED)


def has_submitted(survey, user):
    if FeedbackSubmission.objects.filter(survey=survey, user=user).exists():
        return True
    return _live_receipts().filter(survey=survey, user=user).exists()


def received_counts(survey_ids):
    survey_ids = list(survey_ids)
    counts = dict.fromkeys(survey_ids, 0)
    for row in (
        FeedbackSubmission.objects.filter(survey_id__in=survey_ids).values("survey_id").annotate(total=Count("id"))
    ):
        counts[row["survey_id"]] = row["total"]
    known = FeedbackSubmission.objects.filter(survey_id__in=survey_ids).values("idempotency_key")
    for row in (
        _live_receipts()
        .filter(survey_id__in=survey_ids)
        .exclude(submission_uuid__in=known)
        .values("survey_id")
        .annotate(total=Count("id"))
    ):
        counts[row["survey_id"]] += row["total"]
    return counts


def receipt_rows(user):
    """Customer-home rows for inbox replies, shaped like serialize_submission() without answers."""

    known = FeedbackSubmission.objects.filter(user=user).values("idempotency_key")
    rows = []
    receipts = (
        _live_receipts()
        .filter(user=user)
        .exclude(submission_uuid__in=known)
        .select_related("survey", "survey__category")
        .order_by("-submitted_at")
    )
    for receipt in receipts:
        category = receipt.survey.category
        rows.append({
            "id": None,
            "submitted_at": receipt.submitted_at.isoformat(),
            "submitted_date": format_payload_date(receipt.submitted_at),
            "submitted_datetime": format_payload_datetime(receipt.submitted_at),
            "consent_follow_up": receipt.consent_follow_up,
            "respondent_email": user.email,
            "display_name": user.get_full_name() or user.username,
            "survey": {
                "id": receipt.survey.id,
                "title": receipt.survey.title,
                "slug": receipt.survey.slug,
                "category": {"id": category.id, "name": category.name} if category else None,
            },
            "answers": {"count": None},
            "via_inbox": True,
        })
    return rows
