"""Hard delete of a test or simulated survey and everything tied to it (spec §7.4).

The order follows the relations that would otherwise stop the delete (PROTECT)
or leave orphans (SET_NULL on ImprovementUpdate.survey).  Inbox rows follow the
global lock order shared with ACK and abandon: receipt, then body, then counter.
A dry run executes the same deletes and rolls back, so its counts are exact.
"""

from collections import Counter, defaultdict

from django.apps import apps
from django.db import transaction
from django.db.models import F

from cloudapi.models import (
    InboxCounter,
    InboxSubmission,
    PublishedResultRecord,
    SubmissionReceipt,
    SurveyChange,
    SurveyDefinitionRevision,
)

from .analysis_jobs import suppress_analysis_scheduling
from .models import (
    ImportedSubmissionSource,
    ImprovementNotice,
    ImprovementUpdate,
    SurveyAnalysisSource,
    Survey,
)


class PurgeRefused(Exception):
    pass


def _delete(queryset, counts):
    _total, breakdown = queryset.delete()
    counts.update(breakdown)


def _delete_survey_row(survey, counts):
    _delete(Survey.objects.filter(pk=survey.pk), counts)


def _lock_receipts(survey):
    list(SubmissionReceipt.objects.select_for_update().filter(survey=survey).order_by("pk").values_list("pk", flat=True))


def _release_inbox(survey, counts):
    _lock_receipts(survey)
    bodies = list(
        InboxSubmission.objects.select_for_update().filter(survey=survey).order_by("pk").values_list("pk", "node_id", "size_bytes")
    )
    released = defaultdict(lambda: [0, 0])
    for _pk, node_id, size in bodies:
        released[node_id][0] += 1
        released[node_id][1] += size
    _delete(InboxSubmission.objects.filter(pk__in=[pk for pk, _node, _size in bodies]), counts)
    for node_id, (count, size) in released.items():
        InboxCounter.objects.filter(node_id=node_id).update(
            occupied_count=F("occupied_count") - count, occupied_bytes=F("occupied_bytes") - size
        )
    _delete(SubmissionReceipt.objects.filter(survey=survey), counts)


def _refuse_if_protected(survey):
    if survey.owner_node_id:
        raise PurgeRefused("指派給節點的問卷不能清除")
    if apps.is_installed("cloudsync"):
        from cloudsync.models import SurveySyncState

        if SurveySyncState.objects.filter(survey=survey).exists():
            raise PurgeRefused("由雲端同步的問卷不能在本機清除")


def purge_survey(survey, *, dry_run=False):
    counts = Counter()
    with suppress_analysis_scheduling(), transaction.atomic():
        survey = Survey.objects.select_for_update().get(pk=survey.pk)
        _refuse_if_protected(survey)
        reply_keys = list(survey.submissions.values_list("idempotency_key", flat=True))

        _delete(ImprovementNotice.objects.filter(improvement__survey=survey), counts)
        _delete(ImprovementUpdate.objects.filter(survey=survey), counts)
        _delete(ImportedSubmissionSource.objects.filter(batch__survey=survey), counts)
        SurveyAnalysisSource.objects.filter(survey=survey).update(active_external_version=None)
        _release_inbox(survey, counts)
        _delete(PublishedResultRecord.objects.filter(survey=survey), counts)
        _delete(SurveyChange.objects.filter(survey=survey), counts)
        _delete(SurveyDefinitionRevision.objects.filter(survey=survey), counts)
        if apps.is_installed("cloudsync"):
            from cloudsync.models import PendingAck

            _delete(PendingAck.objects.filter(submission_uuid__in=reply_keys), counts)
        if apps.is_installed("node"):
            from node.models import NodeAIGrant

            # Grants PROTECT their job; the append-only audit log keeps the authorization history.
            _delete(NodeAIGrant.objects.filter(job__survey=survey), counts)
        _delete_survey_row(survey, counts)
        if dry_run:
            transaction.set_rollback(True)
    return dict(counts)
