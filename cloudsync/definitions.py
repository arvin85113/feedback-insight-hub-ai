"""Apply cloud survey definitions to the node's copy (spec §5 定義同步).

`upsert_definition` is the only way a definition reaches the node's models — background
sync, node edits and node survey creation all call it.  Version check, survey and question
writes and the revision are one transaction under a row lock, so a half-applied definition
is never left behind and an older reply can never overwrite a newer one.
"""

import logging

from django.db import IntegrityError, transaction

from cloudapi.definition import apply_definition, validate_definition
from cloudapi.models import SurveyDefinitionRevision
from feedback.models import Survey

from .client import GONE, CloudError
from .models import CloudLink, StaleLink, SurveySyncState

logger = logging.getLogger(__name__)
PAGE_SIZE = 50


def _free_slug(slug, survey_uuid):
    clash = Survey.objects.select_for_update().filter(slug=slug).exclude(uuid=survey_uuid).first()
    if clash is not None:
        clash.slug = f"{slug[:30]}-local-{clash.pk}"
        clash.save(update_fields=["slug"])
        logger.warning("local survey %s renamed to %s to make room for a synced survey", clash.pk, clash.slug)


def _upsert_locked(definition, version):
    survey = Survey.objects.select_for_update().filter(uuid=definition["survey_uuid"]).first()
    if survey is not None and survey.definition_version >= version:
        return survey, False
    if survey is None or survey.slug != definition["slug"]:
        _free_slug(definition["slug"], definition["survey_uuid"])
    created = survey is None
    if created:
        survey = Survey(uuid=definition["survey_uuid"])
    apply_definition(survey, definition, version=version)
    if created:
        # Marks the survey as a cloud copy before the first sync cycle (builder spec §7.4).
        SurveySyncState.objects.get_or_create(survey=survey)
    SurveyDefinitionRevision.objects.get_or_create(survey=survey, version=version, defaults={"definition": definition})
    return survey, True


def upsert_definition(definition):
    definition = validate_definition(definition)
    version = int(definition["version"])
    try:
        with transaction.atomic():
            return _upsert_locked(definition, version)
    except IntegrityError:
        # Another writer created the same survey uuid first; retry against its row.
        with transaction.atomic():
            return _upsert_locked(definition, version)


def _save_cursor(generation, cursor):
    if not CloudLink.update_if_current(generation, cursor=cursor):
        raise StaleLink()


def _snapshot(client, generation):
    data = client.get("surveys/snapshot/")
    with transaction.atomic():
        applied = sum(upsert_definition(item)[1] for item in data["surveys"])
        _save_cursor(generation, data["cursor"])
    return applied


def sync_definitions(client, link):
    """Pull definition changes. Each page and its cursor commit together; a re-link or
    disconnect during the run raises StaleLink and rolls the current page back."""

    generation = link.generation
    cursor = link.cursor
    if not cursor:
        return _snapshot(client, generation)
    applied = 0
    while True:
        try:
            page = client.get("surveys/changes/", params={"cursor": cursor, "limit": PAGE_SIZE})
        except CloudError as exc:
            if exc.kind != GONE:
                raise
            _save_cursor(generation, "")
            return applied + _snapshot(client, generation)
        with transaction.atomic():
            applied += sum(upsert_definition(change["definition"])[1] for change in page["changes"])
            _save_cursor(generation, page["next_cursor"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            return applied
