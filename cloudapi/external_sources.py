"""Bounded external-source contract; file paths and review bodies never cross this API."""

import re
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from feedback.analysis_jobs import suppress_analysis_scheduling
from feedback.analysis_sources import register_external_dataset_version, resolve_analysis_source
from feedback.models import Survey

from .definition import apply_definition, frozen_fields_changed, serialize_definition, validate_definition
from .errors import DefinitionError, VersionConflict
from .models import NodeDevice, SurveyDefinitionRevision

TEXT_FIELDS = {
    "source_ref": 255, "source_version": 255, "source_revision": 128, "cleaning_version": 100,
    "content_sha256": 64, "schema_sha256": 64, "mapping_key": 100, "mapping_version": 100,
}
PROVENANCE_FIELDS = {"dataset_url", "license_name", "viewer_conversion_revision", "config", "split"}
FIELDS = {*TEXT_FIELDS, "row_count", "source_latest_at", "provenance"}


def validate_registration(value):
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise DefinitionError("來源 metadata 欄位不完整或包含未允許欄位")
    out = dict(value)
    for key, limit in TEXT_FIELDS.items():
        text = out[key]
        if not isinstance(text, str) or (not text and key != "schema_sha256") or text != text.strip() or len(text) > limit:
            raise DefinitionError("來源版本欄位格式錯誤")
        if any(ord(c) < 32 for c in text):
            raise DefinitionError("來源版本欄位含控制字元")
    for key in ("content_sha256", "schema_sha256"):
        if key == "schema_sha256" and out[key] == "":
            continue
        if not re.fullmatch(r"[0-9a-f]{64}", out[key]):
            raise DefinitionError("來源 SHA-256 格式錯誤")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", out["mapping_key"]):
        raise DefinitionError("mapping 識別碼格式錯誤")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", out["source_ref"]):
        raise DefinitionError("來源必須是 repo 識別碼，不接受檔案路徑")
    for key in ("source_version", "source_revision", "cleaning_version", "mapping_version"):
        if not re.fullmatch(r"[A-Za-z0-9._:-]+", out[key]):
            raise DefinitionError("版本識別碼不能包含路徑或正文")
    if isinstance(out["row_count"], bool) or not isinstance(out["row_count"], int) or not 1 <= out["row_count"] <= 2**63 - 1:
        raise DefinitionError("資料筆數必須是有效正整數")
    date = out["source_latest_at"]
    if date is not None:
        try:
            date = parse_datetime(date) if isinstance(date, str) and len(date) <= 64 else None
        except ValueError:
            date = None
        if date is None or timezone.is_naive(date):
            raise DefinitionError("來源時間必須包含時區")
        out["source_latest_at"] = date.isoformat()
    provenance = out["provenance"]
    if not isinstance(provenance, dict) or set(provenance) - PROVENANCE_FIELDS:
        raise DefinitionError("來源說明含未允許欄位")
    if any(not isinstance(v, str) or len(v) > 1000 for v in provenance.values()):
        raise DefinitionError("來源說明超出長度限制")
    url = provenance.get("dataset_url")
    if url:
        try:
            parts = urlsplit(url)
        except ValueError as exc:
            raise DefinitionError("來源網址格式錯誤") from exc
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise DefinitionError("來源網址必須是無憑證的 HTTPS 網址")
    return out


def registration_for_version(version):
    return validate_registration({
        **{key: getattr(version, key) for key in TEXT_FIELDS},
        "row_count": version.row_count,
        "source_latest_at": version.source_latest_at.isoformat() if version.source_latest_at else None,
        "provenance": {key: value for key, value in version.provenance.items() if key in PROVENANCE_FIELDS},
    })


def registration_kwargs(value):
    out = validate_registration(value)
    if out["source_latest_at"]:
        out["source_latest_at"] = parse_datetime(out["source_latest_at"])
    return out


@transaction.atomic
def register_node_dataset(node, *, definition, registration, expected_version):
    """Create/select a source and freeze its definition in one cloud transaction.

    Exact retries are checked before the expected version. A changed source requires
    the current version; replaying an older registered source cannot reactivate it.
    """
    from .writes import random_slug, record_version

    definition = validate_definition(definition)
    registration = validate_registration(registration)
    if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
        raise DefinitionError("expected_version 必須是非負整數")
    if not definition["questions"] or definition.get("external_source") not in (None, registration):
        raise DefinitionError("外部問卷定義與來源不一致")
    # Serializes creation where there is not yet a Survey row to lock.
    device = NodeDevice.objects.select_for_update().get(pk=node.pk)
    if device.status != NodeDevice.Status.ACTIVE:
        raise PermissionError()
    survey = Survey.objects.select_for_update().filter(uuid=definition["survey_uuid"]).first()
    created = survey is None
    if not created:
        if survey.owner_node_id != node.pk:
            raise PermissionError()
        binding = resolve_analysis_source(survey, lock=True)
        if not binding.is_external:
            raise DefinitionError("不能將現有填答問卷轉為外部資料問卷")
        current = serialize_definition(survey)
        proposed = {**definition, "published": True}
        assigned_codes = {item["uuid"]: item["code"] for item in current["questions"]}
        proposed["questions"] = [
            {**item, "code": item["code"] or assigned_codes.get(str(item["uuid"]), "")}
            for item in definition["questions"]
        ]
        if frozen_fields_changed(current, {**proposed, "external_source": current["external_source"]}):
            raise DefinitionError("mapping 題目已改變，請建立另一份外部問卷")
        if current["external_source"] == registration:
            return SurveyDefinitionRevision.objects.get(survey=survey, version=survey.definition_version), False
        if survey.definition_version != expected_version:
            raise VersionConflict(survey.definition_version)
    elif expected_version != 0:
        raise VersionConflict(0)
    else:
        survey = Survey(uuid=definition["survey_uuid"], owner_node=device, slug=random_slug())
    version = survey.definition_version + 1 if not created else 1
    fixed = {**(proposed if not created else definition), "slug": survey.slug, "is_active": False, "published": True,
             "published_version": survey.published_version or version,
             "published_at": survey.published_at.isoformat() if survey.published_at else timezone.now().isoformat(),
             "analysis_definition_version": version, "thank_you_email_enabled": False,
             "improvement_tracking_enabled": False}
    with suppress_analysis_scheduling():
        apply_definition(survey, fixed, version=version)
        try:
            register_external_dataset_version(survey.pk, **registration_kwargs(registration))
        except (ValueError, ValidationError) as exc:
            raise DefinitionError("同一來源版本不可修改 metadata") from exc
        survey.refresh_from_db()
        revision = record_version(survey)
    return revision, created
