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
from feedback.analysis_sources import external_version_identity, resolve_analysis_source
from feedback.models import ExternalDatasetVersion, Survey, SurveyAnalysisState

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
    if published_at.tzinfo is None:
        raise ResultInvalid("published_at must include a time zone")
    _validate_source(survey, content)
    return publish_uuid, survey, definition_version, watermark, published_at


def _validate_source(survey, content):
    binding = resolve_analysis_source(survey)
    identity = content.get("input_source", {"kind": "answers"})
    if not isinstance(identity, dict):
        raise ResultInvalid("input_source must be an object")
    if identity.get("kind") == "answers":
        if binding.is_external:
            raise ResultInvalid("external survey requires a registered input_source")
        if identity != {"kind": "answers"}:
            raise ResultInvalid("unexpected answer source metadata")
        return identity
    if identity.get("kind") != "external" or not binding.is_external:
        raise ResultInvalid("input_source does not match survey kind")
    fields = {"kind", "source_ref", "source_version", "content_sha256", "schema_sha256", "mapping_key", "mapping_version"}
    if set(identity) != fields or any(not isinstance(value, str) or len(value) > 255 for value in identity.values()):
        raise ResultInvalid("invalid external source metadata")
    version = ExternalDatasetVersion.objects.filter(
        source__survey=survey,
        source_ref=identity.get("source_ref"),
        source_version=identity.get("source_version"),
    ).first()
    if version is None or identity != external_version_identity(version):
        raise ResultInvalid("input_source is not a registered immutable version")
    if content["analyzed_through_sequence"] != 0:
        raise ResultInvalid("external input cannot have an inbox watermark")
    return identity


def apply_upload(node, *, publish_uuid, publish_sequence, content_hash, content):
    publish_uuid, survey, definition_version, watermark, published_at = _validate(
        node, publish_uuid, publish_sequence, content_hash, content
    )
    conflict_id = None
    with transaction.atomic():
        # Source registration locks this same parent. Recheck ownership/versions under it,
        # not a cached Survey or an earlier query outside the publication transaction.
        survey = Survey.objects.select_for_update().filter(pk=survey.pk, owner_node=node).first()
        if survey is None:
            raise PermissionError("survey does not belong to this node")
        input_source = _validate_source(survey, content)
        if definition_version > survey.definition_version or watermark > survey.response_sequence:
            raise ResultInvalid("result versions are ahead of the cloud")
        SurveyAnalysisState.objects.get_or_create(survey=survey)
        state = SurveyAnalysisState.objects.select_for_update().get(survey=survey)
        existing = PublishedResultRecord.objects.filter(publish_uuid=publish_uuid).first()
        if existing is not None and existing.node_id != node.pk:
            raise ResultConflict()  # another node's record: never touch its counter
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
            if input_source["kind"] == "external":
                binding = resolve_analysis_source(survey, lock=True)
                newer = newer and (
                    input_source["source_ref"] == binding.source_ref
                    and input_source["source_version"] == binding.source_version
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
                "input_source": input_source,
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
