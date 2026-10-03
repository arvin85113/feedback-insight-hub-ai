"""Freeze the reply watermark for one analysis run (spec §7 輸入擷取與水位綁定).

Inbox replies up to the watermark W are all committed (or abandoned) when W is read
and never change afterwards; anything synced later has a higher sequence. Filtering
every read by `response_sequence <= W` therefore gives the run a fixed inbox input.
"""

from dataclasses import dataclass, field

from django.db.models import Q

from feedback.models import FeedbackSubmission

from .models import SurveySyncState


@dataclass
class CaptureScope:
    watermark: int
    definition_version: int
    submission_filter: Q
    excluded: dict = field(default_factory=dict)


def capture_scope(survey):
    watermark = (
        SurveySyncState.objects.filter(survey=survey).values_list("synced_through_sequence", flat=True).first() or 0
    )
    submission_filter = Q(synced_source__isnull=True) | Q(synced_source__response_sequence__lte=watermark)
    in_scope = FeedbackSubmission.objects.filter(survey=survey).filter(submission_filter)
    excluded = {
        "voided": in_scope.filter(voided_at__isnull=False).count(),
        "incomplete": in_scope.filter(voided_at__isnull=True, is_complete=False).count(),
    }
    return CaptureScope(watermark, survey.definition_version, submission_filter, excluded)
