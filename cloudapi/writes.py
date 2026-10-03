"""Cloud-side writes to node-owned survey definitions.

Every change locks the survey row, checks the expected version and the
semantic lock, writes the models, then records an immutable revision and a
SurveyChange whose sequence number comes from the locked ChangeClock row, so
sequence order equals commit order (spec §4).
"""

from django.db import transaction
from django.utils.text import slugify

from feedback.models import Question, Survey

from .definition import SEMANTIC_FIELDS, apply_definition, serialize_definition, validate_definition
from .errors import SemanticLockViolation, VersionConflict
from .models import ChangeClock, SurveyChange, SurveyDefinitionRevision


def tick_clock():
    clock = ChangeClock.objects.select_for_update().get(pk=1)
    clock.value += 1
    clock.save(update_fields=["value"])
    return clock.value


def record_revision(survey):
    """Freeze the definition written in this transaction. Callers return this revision's
    definition instead of re-serializing later, so a reply never mixes versions."""

    revision = SurveyDefinitionRevision.objects.create(
        survey=survey, version=survey.definition_version, definition=serialize_definition(survey)
    )
    SurveyChange.objects.create(seq=tick_clock(), survey=survey, definition_version=survey.definition_version)
    return revision


def unique_slug(text):
    base = (slugify(text) or "survey")[:40]
    slug, counter = base, 2
    while Survey.objects.filter(slug=slug).exists():
        slug = f"{base}-{counter}"
        counter += 1
    return slug


def check_semantic_lock(survey, definition):
    incoming = {str(item["uuid"]): item for item in definition["questions"]}
    for question in survey.questions.filter(has_received_answer=True):
        item = incoming.get(str(question.uuid))
        if item is not None and any(item[field] != getattr(question, field) for field in SEMANTIC_FIELDS):
            raise SemanticLockViolation(str(question.uuid))


@transaction.atomic
def change_definition(survey_uuid, *, expected_version, definition):
    validate_definition(definition)
    survey = Survey.objects.select_for_update().get(uuid=survey_uuid)
    if survey.definition_version != expected_version:
        raise VersionConflict(survey.definition_version)
    check_semantic_lock(survey, definition)
    apply_definition(survey, {**definition, "survey_uuid": str(survey.uuid), "slug": survey.slug},
                     version=survey.definition_version + 1)
    return record_revision(survey)


@transaction.atomic
def create_node_survey(node, definition):
    validate_definition(definition)
    existing = Survey.objects.select_for_update().filter(uuid=definition["survey_uuid"]).first()
    if existing is not None:
        if existing.owner_node_id != node.pk:
            raise PermissionError("survey belongs to another node")
        revision = SurveyDefinitionRevision.objects.get(survey=existing, version=existing.definition_version)
        return revision, False
    slug = unique_slug(definition["slug"] or definition["title"])
    survey = Survey(uuid=definition["survey_uuid"], owner_node=node, slug=slug)
    apply_definition(survey, {**definition, "slug": slug}, version=1)
    return record_revision(survey), True


@transaction.atomic
def assign_survey_to_node(survey, node):
    survey = Survey.objects.select_for_update().get(pk=survey.pk)
    survey.owner_node = node
    survey.definition_version += 1
    survey.save(update_fields=["owner_node", "definition_version"])
    Question.objects.filter(survey=survey, answers__isnull=False).update(has_received_answer=True)
    return record_revision(survey)
