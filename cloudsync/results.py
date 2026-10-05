"""Node result uploads (spec §5 結果上傳, §7).

A publication is frozen into a ResultUpload inside the publishing transaction, while
the SurveyAnalysisState row is locked: identity (uuid, sequence) and content hash are
decided once and every resend reuses them.
"""

import json

from django.conf import settings
from django.db import transaction

from cloudapi.envelope import canonical_bytes, sha256_hex
from feedback.analysis_sources import (
    AnalysisSourceConfigurationError, external_version_identity, resolve_analysis_source,
)
from feedback.models import ExternalDatasetVersion, SurveyAnalysisState

from .client import TRANSIENT, UNAUTHORIZED, CloudError
from .models import ResultUpload, SurveySyncState
from .scope import is_cloud_synced

STAGES = ("statistics", "text")
VERSION_KEYS = ("input_version", "config_version", "pipeline_version")


def _stage(manifest, key, snapshot_id):
    item = manifest.get(key) or {}
    stage = {name: item.get(name) for name in VERSION_KEYS}
    stage["current"] = bool(item) and item.get("snapshot_id") == snapshot_id
    return stage


def build_content(state):
    snapshot = state.published_snapshot
    scope = ((snapshot.source_snapshot or {}).get("data_scope") or {}) if snapshot else {}
    manifest = state.publication_manifest or {}
    stages = {key: _stage(manifest, key, state.published_snapshot_id) for key in STAGES}

    ai = _stage(manifest, "ai", state.published_snapshot_id)
    ai["model_name"] = (manifest.get("ai") or {}).get("model_name")
    base = stages["statistics"]
    ai["current"] = bool(
        ai["current"] and base["current"] and all(ai[name] == base[name] for name in VERSION_KEYS)
    )
    stages["ai"] = ai
    ai_payload, ai_source = None, {}
    if ai["current"] and state.published_ai_payload:
        ai_payload = state.published_ai_payload
        stage = state.published_ai_stage
        if stage is not None:
            source = stage.snapshot
            ai_source = {
                "response_count": source.response_count,
                "analysis_coverage": float(source.analysis_coverage),
                "source_latest_at": source.source_latest_at.isoformat() if source.source_latest_at else None,
                "model_name": stage.model_name,
            }

    content = {
        "survey_uuid": str(state.survey.uuid),
        "definition_version": scope.get("definition_version", 0),
        "analyzed_through_sequence": scope.get("analyzed_through_sequence", 0),
        "input_fingerprint": snapshot.data_fingerprint if snapshot else "",
        "published_at": state.published_at.isoformat() if state.published_at else None,
        "pipeline": {
            # The versions the shown result was computed with, not the state's current ones.
            "input_version": base["input_version"],
            "config_version": base["config_version"],
            "pipeline_version": base["pipeline_version"],
            "implementation_version": scope.get("pipeline_implementation_version", "") if snapshot else "",
        },
        "stages": stages,
        "display_payload": state.published_display_payload or {},
        "ai_payload": ai_payload,
        "ai_source": ai_source,
        "coverage": {
            "analyzed_unique": snapshot.response_count if snapshot else 0,
            "excluded": scope.get("excluded") or {"voided": 0, "incomplete": 0},
        },
    }
    binding = resolve_analysis_source(state.survey)
    if binding.is_external:
        # Identity comes from the published snapshot/stage, never today's active binding.
        source_stage = manifest.get("statistics") or {}
        version = ExternalDatasetVersion.objects.filter(
            source__survey=state.survey,
            source_ref=source_stage.get("source_ref"),
            source_version=source_stage.get("source_version"),
        ).first()
        if version is None or scope.get("source_version") != version.source_version:
            raise AnalysisSourceConfigurationError("外部發布結果缺少固定來源版本")
        content["input_source"] = external_version_identity(version)
        content["analyzed_through_sequence"] = 0
    else:
        content["input_source"] = {"kind": "answers"}
    # Hash what the cloud will see after JSON round-tripping (tuples, non-string keys, ...).
    return json.loads(json.dumps(content, ensure_ascii=False))


def record_publication(state):
    """Freeze the current publication; the caller holds the SurveyAnalysisState row lock."""

    if not is_cloud_synced(state.survey):
        return None
    SurveySyncState.objects.get_or_create(survey=state.survey)
    sync_state = SurveySyncState.objects.select_for_update().get(survey=state.survey)
    sequence = max(sync_state.cloud_publish_sequence, sync_state.local_publish_sequence) + 1
    sync_state.local_publish_sequence = sequence
    sync_state.save(update_fields=["local_publish_sequence"])
    content = build_content(state)
    return ResultUpload.objects.create(
        survey=state.survey,
        publish_sequence=sequence,
        content_hash=sha256_hex(content),
        content=content,
        published_at=state.published_at,
    )


def backfill_publications():
    """Create uploads for publications that have none yet (e.g. made while this code was absent)."""

    created = 0
    candidates = (
        SurveyAnalysisState.objects.filter(published_at__isnull=False, survey__definition_revisions__isnull=False)
        .values_list("survey_id", flat=True)
        .distinct()
    )
    for survey_id in candidates:
        with transaction.atomic():
            state = (
                SurveyAnalysisState.objects.select_for_update()
                .select_related("survey", "published_snapshot", "published_ai_stage__snapshot")
                .get(survey_id=survey_id)
            )
            if state.published_at is None:
                continue
            if ResultUpload.objects.filter(survey_id=survey_id, published_at=state.published_at).exists():
                continue
            if record_publication(state) is not None:
                created += 1
    return created


RETRYABLE = (TRANSIENT, UNAUTHORIZED)


def upload_results(client):
    """Send pending uploads in sequence order; transient or auth errors stop the cycle and keep them pending."""

    counts = {"uploaded": 0, "stale": 0, "failed": 0}
    for upload in ResultUpload.objects.filter(status=ResultUpload.Status.PENDING).order_by("publish_sequence", "pk"):
        data = canonical_bytes({
            "publish_uuid": str(upload.publish_uuid),
            "publish_sequence": upload.publish_sequence,
            "content_hash": upload.content_hash,
            "content": upload.content,
        })
        if len(data) > settings.CLOUD_RESULT_MAX_BYTES:
            _mark(upload, ResultUpload.Status.FAILED, "too_large")
            counts["failed"] += 1
            continue
        try:
            reply = client.post_raw("results/", data)
        except CloudError as error:
            if error.kind in RETRYABLE:
                ResultUpload.objects.filter(pk=upload.pk).update(attempts=upload.attempts + 1, last_error=error.kind)
                raise
            _mark(upload, ResultUpload.Status.FAILED, REJECTION_NAMES.get(error.status, error.kind))
            counts["failed"] += 1
            continue
        status = reply.get("status") if isinstance(reply, dict) else None
        if status == "applied":
            _mark(upload, ResultUpload.Status.UPLOADED, "")
            counts["uploaded"] += 1
        elif status == "stale":
            _mark(upload, ResultUpload.Status.STALE, "")
            counts["stale"] += 1
        else:
            _mark(upload, ResultUpload.Status.FAILED, "bad_reply")
            counts["failed"] += 1
    return counts


REJECTION_NAMES = {400: "invalid", 404: "not_found", 413: "too_large"}
SETTLED = (ResultUpload.Status.UPLOADED, ResultUpload.Status.STALE)


def _mark(upload, status, error):
    fields = {"status": status, "last_error": error, "attempts": upload.attempts + 1}
    if status in SETTLED:
        # Never resent once settled; keep the hash as the record and drop the bulky copy.
        fields["content"] = {}
    ResultUpload.objects.filter(pk=upload.pk).update(**fields)


def retry_failed():
    return ResultUpload.objects.filter(status=ResultUpload.Status.FAILED).update(
        status=ResultUpload.Status.PENDING, last_error="", attempts=0
    )
