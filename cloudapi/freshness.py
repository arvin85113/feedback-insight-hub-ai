"""Freshness of a node-owned survey's shown result (spec §7 新鮮度分三項判定).

Definition and input freshness are verified by the cloud (definition version, reply
watermark against receipts); the pipeline is only what the node declared.
"""

from feedback.models import FeedbackSubmission

from .models import SubmissionReceipt


def node_freshness(survey, state):
    has_result = bool(state is not None and state.published_upload_uuid)
    watermark = state.analyzed_through_sequence if has_result else 0
    manifest = (state.publication_manifest or {}) if has_result else {}
    definition_current = has_result and state.definition_version == survey.definition_version
    pending_new = (
        SubmissionReceipt.objects.filter(survey=survey, response_sequence__gt=watermark)
        .exclude(status=SubmissionReceipt.Status.ABANDONED)
        .count()
    )
    return {
        "has_result": has_result,
        "publish_sequence": state.publish_sequence if has_result else 0,
        "definition_current": definition_current,
        "pending_new": pending_new,
        "legacy_unmigrated": FeedbackSubmission.objects.filter(survey=survey).count(),
        "is_latest": bool(definition_current and pending_new == 0),
        "coverage": manifest.get("coverage") or {},
        "pipeline_declared": manifest.get("pipeline") or {},
    }
