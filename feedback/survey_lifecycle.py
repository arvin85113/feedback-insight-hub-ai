"""Survey lifecycle operations shared by the cloud website and the node console (builder spec §4, §7.1).

Pages never write survey models directly: they edit a definition dict and hand
it to `commit`, which goes to `cloudapi.writes` on the cloud or to the cloud API
from a node.  Creating, copying and deleting follow the same single path.
"""

import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from cloudapi.definition import archive_survey, blank_definition, serialize_definition
from cloudapi.errors import VersionConflict

from .models import Survey
from .survey_purge import purge_survey

BLANK_FIELDS = ("title", "description", "category", "is_active", "analysis_enabled", "thank_you_email_enabled")
COPY_SUFFIX = "（複本）"


def commit(survey, definition, expected_version):
    if settings.IS_NODE:
        from cloudsync.survey_write import node_commit

        node_commit(survey, definition, expected_version)
    else:
        from cloudapi.writes import change_definition

        change_definition(survey.uuid, expected_version=expected_version, definition=definition)


def _create(definition):
    if settings.IS_NODE:
        from cloudsync.survey_write import create_survey

        return create_survey(definition)
    from cloudapi.writes import create_survey

    return create_survey(definition).survey


def create_draft(data):
    """A new draft at version 1; `data` holds a title and optional survey settings or a full definition."""

    if "questions" in data:
        definition = dict(data)
    else:
        definition = blank_definition(
            data.get("survey_uuid") or uuid.uuid4(),
            **{field: data[field] for field in BLANK_FIELDS if field in data},
        )
    return _create(definition)


def copy_as_draft(survey):
    """A new, unpublished survey with the same questions; codes, choices and counters are kept (spec §4.4)."""

    source = serialize_definition(survey)
    definition = blank_definition(
        uuid.uuid4(),
        title=f"{source['title']}{COPY_SUFFIX}"[:255],
        description=source["description"],
        category=source["category"],
        analysis_enabled=source["analysis_enabled"],
        thank_you_email_enabled=source["thank_you_email_enabled"],
        improvement_tracking_enabled=source["improvement_tracking_enabled"],
    )
    definition["next_question_number"] = source["next_question_number"]
    definition["questions"] = [
        {**item, "uuid": str(uuid.uuid4())} for item in source["questions"] if item["is_active"]
    ]
    return _create(definition)


def _is_cloud_copy_on_node(survey):
    if not settings.IS_NODE:
        return False
    from cloudsync.models import SurveySyncState

    return SurveySyncState.objects.filter(survey=survey).exists()


@transaction.atomic
def delete_or_archive(survey, expected_version):
    """Unassigned drafts are deleted; published surveys and node drafts are archived (spec §5.1, §7.1).

    Returns "deleted" or "archived". The draft check is repeated under the survey row lock.
    """

    locked = Survey.objects.select_for_update().get(pk=survey.pk)
    if locked.definition_version != expected_version:
        raise VersionConflict(locked.definition_version)
    deletable = (
        locked.published_version is None
        and not locked.owner_node_id
        and not _is_cloud_copy_on_node(locked)
        # Drafts never have replies; a legacy survey that does is archived, never hard-deleted.
        and not locked.submissions.exists()
    )
    if deletable:
        purge_survey(locked)
        return "deleted"
    definition = serialize_definition(locked)
    archive_survey(definition, timezone.now())
    commit(locked, definition, expected_version)
    return "archived"
