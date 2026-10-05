"""Controlled writes to survey definitions (survey builder spec §7.1).

Every entry point goes through `_locked_write`: lock the survey row, check the
expected version and the lifecycle rules, write the models, then record an
immutable revision (every version, every survey) and, for node-owned surveys,
a SurveyChange whose sequence comes from the locked ChangeClock row so sequence
order equals commit order.
"""

import contextlib
import secrets
import string

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from feedback.analysis_jobs import suppress_analysis_scheduling
from feedback.models import Survey

from .definition import apply_definition, frozen_fields_changed, serialize_definition, validate_definition
from .errors import DefinitionError, PublishBlocked, PublishedLocked, VersionConflict
from .models import ChangeClock, SurveyChange, SurveyDefinitionRevision

SLUG_ALPHABET = string.ascii_lowercase + string.digits
ANALYSIS_FIELDS = ("analysis_enabled", "archived_at")


def tick_clock():
    clock = ChangeClock.objects.select_for_update().get(pk=1)
    clock.value += 1
    clock.save(update_fields=["value"])
    return clock.value


def record_version(survey):
    """Freeze the definition written in this transaction. Callers return this revision's
    definition instead of re-serializing later, so a reply never mixes versions."""

    revision = SurveyDefinitionRevision.objects.create(
        survey=survey, version=survey.definition_version, definition=serialize_definition(survey)
    )
    if survey.owner_node_id:
        SurveyChange.objects.create(seq=tick_clock(), survey=survey, definition_version=survey.definition_version)
    return revision


def random_slug():
    while True:
        slug = "".join(secrets.choice(SLUG_ALPHABET) for _ in range(8))
        if not Survey.objects.filter(slug=slug).exists():
            return slug


def _locked_write(survey, *, expected_version, definition, prepare=None):
    """Shared body of every controlled entry point. `survey` is locked (or new and unsaved)."""

    if expected_version is not None and survey.definition_version != expected_version:
        raise VersionConflict(survey.definition_version)
    if prepare is not None:
        definition = prepare(survey, definition)
    version = survey.definition_version + 1 if survey.pk else 1
    draft = survey.pk is None or survey.published_version is None
    # Drafts have no replies, so their edits and their publish schedule no analysis (spec §7.2).
    with suppress_analysis_scheduling() if draft else contextlib.nullcontext():
        apply_definition(survey, definition, version=version)
    return record_version(survey)


def _check_choice_codes(survey, definition):
    """An existing question keeps its codes; new options arrive without one (builder spec §2.1)."""

    existing = {str(q.uuid): ({c["code"] for c in q.choices}, q.next_choice_number) for q in survey.questions.all()}
    for item in definition["questions"]:
        if str(item["uuid"]) not in existing:
            continue
        known, next_number = existing[str(item["uuid"])]
        for choice in item["choices"]:
            code = choice.get("code") or ""
            # A code the question never had must be fresh (at or above its counter): deleted codes stay retired.
            if code and code not in known and int(code[1:]) < next_number:
                raise DefinitionError("選項代碼不可重複使用")


def _lifecycle(survey, definition):
    """Server-owned publish fields: carried over, or set on the draft → published transition."""

    _check_choice_codes(survey, definition)
    definition = dict(definition)
    current_source = serialize_definition(survey).get("external_source")
    if definition.get("external_source") != current_source:
        raise DefinitionError("來源版本只能經外部資料登錄入口更新")
    next_version = survey.definition_version + 1
    definition.update(
        published_version=survey.published_version,
        published_at=survey.published_at.isoformat() if survey.published_at else None,
        analysis_definition_version=survey.analysis_definition_version,
    )
    if survey.published_version is not None:
        if frozen_fields_changed(serialize_definition(survey), definition):
            raise PublishedLocked()
        current = serialize_definition(survey)
        if any(current[field] != definition[field] for field in ANALYSIS_FIELDS):
            definition["analysis_definition_version"] = next_version
        return definition
    if definition["published"]:
        if not definition["questions"]:
            raise DefinitionError("至少需要一題才能發布")
        if survey.owner_node_id and not current_source and not settings.CLOUD_INBOX_ENABLED:
            raise PublishBlocked()
        definition.update(
            published_version=next_version,
            published_at=timezone.now().isoformat(),
            analysis_definition_version=next_version,
        )
    return definition


@transaction.atomic
def change_definition(survey_uuid, *, expected_version, definition):
    definition = validate_definition(definition)
    survey = Survey.objects.select_for_update().get(uuid=survey_uuid)
    publishing = survey.published_version is None and definition["published"]
    revision = _locked_write(
        survey,
        expected_version=expected_version,
        definition={**definition, "survey_uuid": str(survey.uuid), "slug": survey.slug},
        prepare=_lifecycle,
    )
    if publishing and survey.owner_node_id and not definition.get("external_source"):
        Survey.objects.filter(pk=survey.pk).update(inbox_since=timezone.now())
    return revision


def _draft(definition):
    return {**definition, "published": False, "published_version": None, "published_at": None,
            "analysis_definition_version": None}


@transaction.atomic
def create_survey(definition, *, slug=None):
    """Cloud website: a new draft owned by no node (spec §7.1). Seeds may pass a fixed slug."""

    definition = validate_definition(definition)
    survey = Survey(uuid=definition["survey_uuid"], slug=slug or random_slug())
    return _locked_write(survey, expected_version=None, definition={**_draft(definition), "slug": survey.slug})


@transaction.atomic
def create_imported_survey(definition):
    """Dataset import: a survey created already published at version 1 (builder spec §6, §7.1)."""

    definition = validate_definition(definition)
    survey = Survey(uuid=definition["survey_uuid"], slug=definition["slug"])
    now = timezone.now().isoformat()
    published = {**definition, "published": True, "published_version": 1, "published_at": now,
                 "analysis_definition_version": 1}
    return _locked_write(survey, expected_version=None, definition=published)


@transaction.atomic
def create_node_survey(node, definition):
    definition = validate_definition(definition)
    if definition.get("external_source"):
        raise DefinitionError("外部問卷須經資料集登錄入口建立")
    existing = Survey.objects.select_for_update().filter(uuid=definition["survey_uuid"]).first()
    if existing is not None:
        if existing.owner_node_id != node.pk:
            raise PermissionError("survey belongs to another node")
        revision = SurveyDefinitionRevision.objects.get(survey=existing, version=existing.definition_version)
        return revision, False
    survey = Survey(uuid=definition["survey_uuid"], owner_node=node, slug=random_slug())
    return _locked_write(survey, expected_version=None, definition={**_draft(definition), "slug": survey.slug}), True


@transaction.atomic
def assign_survey_to_node(survey, node):
    survey = Survey.objects.select_for_update().get(pk=survey.pk)
    if survey.published_version is not None:
        raise PublishedLocked()
    survey.owner_node = node
    survey.save(update_fields=["owner_node"])
    return _locked_write(survey, expected_version=survey.definition_version, definition=serialize_definition(survey))
