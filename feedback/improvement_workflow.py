from django.db import transaction
from django.utils import timezone

from .models import ImprovementStatusHistory, ImprovementUpdate


ALLOWED_STATUS_TRANSITIONS = {
    ImprovementUpdate.Status.DRAFT: {
        ImprovementUpdate.Status.PLANNED,
        ImprovementUpdate.Status.ARCHIVED,
    },
    ImprovementUpdate.Status.PLANNED: {
        ImprovementUpdate.Status.DRAFT,
        ImprovementUpdate.Status.IN_PROGRESS,
        ImprovementUpdate.Status.ARCHIVED,
    },
    ImprovementUpdate.Status.IN_PROGRESS: {
        ImprovementUpdate.Status.PLANNED,
        ImprovementUpdate.Status.COMPLETED,
        ImprovementUpdate.Status.ARCHIVED,
    },
    ImprovementUpdate.Status.COMPLETED: {
        ImprovementUpdate.Status.IN_PROGRESS,
        ImprovementUpdate.Status.ARCHIVED,
    },
    ImprovementUpdate.Status.ARCHIVED: set(),
}


class ImprovementTransitionError(ValueError):
    pass


def record_initial_status(improvement, actor):
    return ImprovementStatusHistory.objects.get_or_create(
        improvement=improvement,
        from_status="",
        to_status=improvement.status,
        defaults={"changed_by": actor},
    )[0]


def _restore_target(improvement):
    latest_archive = (
        improvement.status_history.filter(to_status=ImprovementUpdate.Status.ARCHIVED)
        .exclude(from_status="")
        .order_by("-changed_at", "-id")
        .first()
    )
    target = latest_archive.from_status if latest_archive else ImprovementUpdate.Status.PLANNED
    if target == ImprovementUpdate.Status.ARCHIVED or target not in ImprovementUpdate.Status.values:
        return ImprovementUpdate.Status.PLANNED
    return target


def status_targets(improvement):
    if improvement.status == ImprovementUpdate.Status.ARCHIVED:
        target = _restore_target(improvement)
        return [("restore", f"恢復為{ImprovementUpdate.Status(target).label}")]
    return [
        (target, ImprovementUpdate.Status(target).label)
        for target in ImprovementUpdate.Status.values
        if target in ALLOWED_STATUS_TRANSITIONS[improvement.status]
    ]


@transaction.atomic
def transition_improvement(improvement_id, requested_status, actor):
    improvement = ImprovementUpdate.objects.select_for_update().get(pk=improvement_id)
    from_status = improvement.status
    to_status = _restore_target(improvement) if requested_status == "restore" else requested_status

    if from_status == ImprovementUpdate.Status.ARCHIVED:
        if requested_status != "restore":
            raise ImprovementTransitionError("封存項目必須先恢復。")
    elif to_status not in ALLOWED_STATUS_TRANSITIONS.get(from_status, set()):
        raise ImprovementTransitionError("不允許這個狀態轉移。")

    now = timezone.now()
    improvement.status = to_status
    improvement.updated_by = actor
    if to_status == ImprovementUpdate.Status.COMPLETED and from_status != ImprovementUpdate.Status.ARCHIVED:
        improvement.completed_at = now
    elif from_status == ImprovementUpdate.Status.COMPLETED and to_status != ImprovementUpdate.Status.ARCHIVED:
        improvement.completed_at = None
    if to_status == ImprovementUpdate.Status.ARCHIVED:
        improvement.archived_at = now
    elif from_status == ImprovementUpdate.Status.ARCHIVED:
        improvement.archived_at = None
    improvement.save(
        update_fields=[
            "status",
            "updated_by",
            "completed_at",
            "archived_at",
            "updated_at",
        ]
    )
    ImprovementStatusHistory.objects.create(
        improvement=improvement,
        from_status=from_status,
        to_status=to_status,
        changed_by=actor,
    )
    return improvement
