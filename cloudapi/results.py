"""Node result uploads on the cloud (spec §7 結果發布契約).

Every identity-valid upload becomes a metadata-only history row. SurveyAnalysisState is
the single pointer to what the website shows; it moves only while its row is locked and
only when sequence, reply watermark and definition version all move forward or stay.
"""

import uuid
from datetime import datetime

from django.conf import settings
from django.db import transaction
from django.db.models import F

from feedback.analysis_jobs import _bounded_json
from feedback.models import Survey, SurveyAnalysisState

from .envelope import canonical_bytes, sha256_hex
from .models import PublishedResultRecord

CONTENT_DICTS = ("pipeline", "stages", "display_payload", "ai_source", "coverage")


class ResultConflict(Exception):
    """Same publish_uuid with another hash or sequence."""


class ResultInvalid(ValueError):
    pass


def _int(value, name, *, minimum=0):
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ResultInvalid(f"{name} must be an integer >= {minimum}")
    return value


def _uuid(value, name):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ResultInvalid(f"{name} is not a valid UUID") from exc


def _validate(node, publish_uuid, publish_sequence, content_hash, content):
    publish_uuid = _uuid(publish_uuid, "publish_uuid")
    _int(publish_sequence, "publish_sequence", minimum=1)
    if not isinstance(content, dict):
        raise ResultInvalid("content must be an object")
    if len(canonical_bytes(content)) > settings.CLOUD_RESULT_MAX_BYTES:
        raise ResultInvalid("content is too large")
    if not isinstance(content_hash, str) or sha256_hex(content) != content_hash:
        raise ResultInvalid("content_hash does not match content")
    survey_uuid = _uuid(content.get("survey_uuid"), "survey_uuid")
    survey = Survey.objects.filter(uuid=survey_uuid, owner_node=node).first()
    if survey is None:
        raise PermissionError("survey does not belong to this node")
    definition_version = _int(content.get("definition_version"), "definition_version")
    watermark = _int(content.get("analyzed_through_sequence"), "analyzed_through_sequence")
    if watermark > survey.response_sequence:
        raise ResultInvalid("analyzed_through_sequence is ahead of the cloud")
    if definition_version > survey.definition_version:
        raise ResultInvalid("definition_version is ahead of the cloud")
    for key in CONTENT_DICTS:
        if not isinstance(content.get(key), dict):
            raise ResultInvalid(f"{key} must be an object")
    # coverage feeds an integer cast in the overview query; anything else would error on PostgreSQL.
    coverage = content["coverage"]
    _int(coverage.get("analyzed_unique"), "coverage.analyzed_unique")
    excluded = coverage.get("excluded")
    if not isinstance(excluded, dict):
        raise ResultInvalid("coverage.excluded must be an object")
    for key in ("voided", "incomplete"):
        _int(excluded.get(key), f"coverage.excluded.{key}")
    if content.get("ai_payload") is not None and not isinstance(content["ai_payload"], dict):
        raise ResultInvalid("ai_payload must be an object or null")
    try:
        published_at = datetime.fromisoformat(str(content.get("published_at")))
    except ValueError as exc:
        raise ResultInvalid("published_at is not an ISO datetime") from exc
    return publish_uuid, survey, definition_version, watermark, published_at


def apply_upload(node, *, publish_uuid, publish_sequence, content_hash, content):
    publish_uuid, survey, definition_version, watermark, published_at = _validate(
        node, publish_uuid, publish_sequence, content_hash, content
    )
    conflict_id = None
    with transaction.atomic():
        SurveyAnalysisState.objects.get_or_create(survey=survey)
        state = SurveyAnalysisState.objects.select_for_update().get(survey=survey)
        existing = PublishedResultRecord.objects.filter(publish_uuid=publish_uuid).first()
        if existing is not None:
            if existing.content_hash == content_hash and existing.publish_sequence == publish_sequence:
                status = "applied" if state.published_upload_uuid == publish_uuid else "stale"
                return status, False
            conflict_id = existing.pk
        else:
            record = PublishedResultRecord.objects.create(
                publish_uuid=publish_uuid,
                node=node,
                survey=survey,
                publish_sequence=publish_sequence,
                content_hash=content_hash,
                definition_version=definition_version,
                analyzed_through_sequence=watermark,
            )
            newer = (
                publish_sequence > state.publish_sequence
                and watermark >= state.analyzed_through_sequence
                and definition_version >= state.definition_version
            )
            if not newer:
                return "stale", True
            ai_payload = content.get("ai_payload")
            state.published_display_payload = _bounded_json(content["display_payload"])
            state.published_ai_payload = _bounded_json(ai_payload) if ai_payload else {}
            state.publication_manifest = {
                "source": "node",
                "publish_uuid": str(publish_uuid),
                "stages": _bounded_json(content["stages"]),
                "pipeline": _bounded_json(content["pipeline"]),
                "coverage": _bounded_json(content["coverage"]),
                "ai_source": _bounded_json(content["ai_source"]),
                "input_fingerprint": str(content.get("input_fingerprint", ""))[:128],
            }
            state.published_at = published_at
            state.published_snapshot = None
            state.published_ai_stage = None
            state.publish_sequence = publish_sequence
            state.analyzed_through_sequence = watermark
            state.definition_version = definition_version
            state.published_upload_uuid = publish_uuid
            state.save()
            record.applied = True
            record.save(update_fields=["applied"])
            return "applied", True
    # Outside the transaction so the count survives the rejected upload.
    PublishedResultRecord.objects.filter(pk=conflict_id).update(conflict_count=F("conflict_count") + 1)
    raise ResultConflict()
