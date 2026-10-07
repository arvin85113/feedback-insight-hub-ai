"""Register metadata on the cloud first, then atomically bind this node's existing files."""

from datetime import timezone

from django.db import transaction

from cloudapi.errors import DefinitionError
from cloudapi.definition import validate_definition
from cloudapi.external_sources import registration_kwargs, validate_registration
from feedback.external_dataset import file_hash
from feedback.models import Survey
from node.datasets import register_local_dataset

from .client import CloudError
from .definitions import upsert_definition
from .models import CloudLink, StaleLink
from .survey_write import _translate, _client


def register_dataset(verified, definition, *, expected_version, generation, actor=None):
    link = CloudLink.load()
    if link.generation != generation:
        raise StaleLink()
    from cloudapi.models import SurveyDefinitionRevision
    registration = {**verified.registration, "source_latest_at": (
        verified.registration["source_latest_at"].astimezone(timezone.utc).isoformat()
        if verified.registration["source_latest_at"] else None
    )}
    registration = validate_registration(registration)
    client = _client()
    try:
        reply = client.post("datasets/register/", {
            "definition": definition, "registration": registration, "expected_version": expected_version,
        })
    except CloudError as exc:
        raise _translate(exc) from exc
    returned = reply.get("definition") if isinstance(reply, dict) else None
    if not isinstance(returned, dict) or returned.get("survey_uuid") != definition["survey_uuid"]:
        raise DefinitionError("雲端回覆與已驗證來源不一致")
    returned = validate_definition(returned)
    # PostgreSQL/JSON may express the same instant as UTC or Z. All other metadata
    # still compares exactly; a genuinely different source time must be rejected.
    if registration_kwargs(returned.get("external_source")) != registration_kwargs(registration):
        raise DefinitionError("雲端回覆與已驗證來源不一致")
    with transaction.atomic():
        current = CloudLink.objects.select_for_update().get(pk=1)
        if current.generation != generation:
            raise StaleLink()
        if (file_hash(verified.manifest_path) != verified.manifest_sha256
                or file_hash(verified.mapping_path) != verified.mapping_sha256):
            raise DefinitionError("登錄途中本機設定已改變，請重新驗證")
        existing = Survey.objects.select_for_update().filter(uuid=definition["survey_uuid"]).first()
        # Recheck under the lock: another registration may have finished during HTTP.
        local_only = bool(existing and not SurveyDefinitionRevision.objects.filter(survey=existing).exists())
        if local_only and existing.definition_version >= returned["version"]:
            # upsert_definition deliberately skips equal/older versions. Never infer
            # ownership from that no-op or attach an unverified local definition.
            raise DefinitionError("本機未綁定的定義版本與雲端衝突，請人工核對")
        survey, _ = upsert_definition(returned)
        # A background sync may have applied a newer definition while HTTP was in flight.
        if survey.definition_version != returned["version"]:
            raise DefinitionError("本機已取得較新版本，請重新載入")
        source, version, job, changed = register_local_dataset(survey.pk, verified)
        if local_only:
            from feedback.analysis_jobs import schedule_survey_analysis
            # The old result was computed before the cloud definition existed.
            # Recompute against the adopted version; never relabel old evidence.
            schedule_survey_analysis(survey.pk, change="config")
        if actor is not None:
            from node.audit import DATASET_REGISTERED, record
            record(DATASET_REGISTERED, actor=actor, target=survey.uuid, source_ref=version.source_ref,
                   source_version=version.source_version, content_sha256=version.content_sha256,
                   row_count=version.row_count)
        return Survey.objects.get(pk=survey.pk), version
