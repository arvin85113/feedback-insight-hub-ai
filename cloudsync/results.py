"""Node result uploads (spec §5 結果上傳, §7).

A publication is frozen into a ResultUpload inside the publishing transaction, while
the SurveyAnalysisState row is locked: identity (uuid, sequence) and content hash are
decided once and every resend reuses them.
"""

import json

from django.db import transaction

from cloudapi.envelope import sha256_hex
from feedback.models import SurveyAnalysisState

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
            "input_version": state.input_version,
            "config_version": state.config_version,
            "pipeline_version": state.pipeline_version,
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
    for survey_id in SurveyAnalysisState.objects.filter(published_at__isnull=False).values_list("survey_id", flat=True):
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
