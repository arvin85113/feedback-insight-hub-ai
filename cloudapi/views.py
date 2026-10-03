from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from feedback.models import Survey

from .auth import CursorInvalid, make_cursor, node_api, parse_cursor, read_json
from .errors import DefinitionError, SemanticLockViolation, VersionConflict
from .models import ChangeClock, SurveyChange, SurveyDefinitionRevision
from .writes import change_definition, create_node_survey

MAX_PAGE = 200


def _owned(request, survey_uuid):
    return Survey.objects.filter(uuid=survey_uuid, owner_node=request.node_device).first()


@node_api
@require_GET
def survey_snapshot(request):
    device = request.node_device
    # Read the committed clock first, then the definitions (spec §4): every change
    # numbered <= cursor is already visible below; newer ones arrive again via changes.
    clock_value = ChangeClock.objects.values_list("value", flat=True).get(pk=1)
    # Never rebuild a definition from live rows here: return the immutable revision of
    # each survey's version, so a reply can't mix a v1 survey with v2 questions.
    versions = dict(Survey.objects.filter(owner_node=device).values_list("pk", "definition_version"))
    revisions = SurveyDefinitionRevision.objects.filter(survey_id__in=versions).order_by("survey_id")
    definitions = [rev.definition for rev in revisions if versions[rev.survey_id] == rev.version]
    return JsonResponse({"cursor": make_cursor(device, clock_value), "surveys": definitions})


@node_api
@require_GET
def survey_changes(request):
    device = request.node_device
    try:
        seq = parse_cursor(device, request.GET.get("cursor", ""))
    except CursorInvalid:
        return JsonResponse({"error": "cursor_invalid"}, status=410)
    try:
        limit = max(1, min(int(request.GET.get("limit", 50)), MAX_PAGE))
    except ValueError:
        return JsonResponse({"error": "bad_request", "message": "limit 必須是整數"}, status=400)
    clock = ChangeClock.objects.get(pk=1)
    if seq < clock.pruned_through or seq > clock.value:
        return JsonResponse({"error": "cursor_invalid"}, status=410)
    rows = list(
        SurveyChange.objects.filter(seq__gt=seq, survey__owner_node=device).order_by("seq")[: limit + 1]
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    revisions = {
        (revision.survey_id, revision.version): revision.definition
        for revision in SurveyDefinitionRevision.objects.filter(
            survey_id__in={row.survey_id for row in rows}, version__in={row.definition_version for row in rows}
        )
    }
    changes = [{"seq": row.seq, "definition": revisions[(row.survey_id, row.definition_version)]} for row in rows]
    next_seq = rows[-1].seq if rows else seq
    return JsonResponse({"changes": changes, "next_cursor": make_cursor(device, next_seq), "has_more": has_more})


@node_api
@require_GET
def survey_revision(request, survey_uuid, version):
    survey = _owned(request, survey_uuid)
    revision = survey and SurveyDefinitionRevision.objects.filter(survey=survey, version=version).first()
    if not revision:
        return JsonResponse({"error": "not_found"}, status=404)
    return JsonResponse({"definition": revision.definition})


@node_api
@require_POST
def survey_create(request):
    definition = read_json(request)
    try:
        revision, created = create_node_survey(request.node_device, definition)
    except DefinitionError as exc:
        return JsonResponse({"error": "invalid_definition", "message": str(exc)}, status=400)
    except PermissionError:
        return JsonResponse({"error": "not_found"}, status=404)
    return JsonResponse({"definition": revision.definition}, status=201 if created else 200)


@node_api
@require_http_methods(["PUT"])
def survey_update(request, survey_uuid):
    if _owned(request, survey_uuid) is None:
        return JsonResponse({"error": "not_found"}, status=404)
    body = read_json(request)
    try:
        expected = int(body["expected_version"])
        definition = body["definition"]
    except (KeyError, TypeError, ValueError):
        return JsonResponse({"error": "bad_request", "message": "需要 expected_version 與 definition"}, status=400)
    try:
        revision = change_definition(survey_uuid, expected_version=expected, definition=definition)
    except DefinitionError as exc:
        return JsonResponse({"error": "invalid_definition", "message": str(exc)}, status=400)
    except VersionConflict as exc:
        return JsonResponse({"error": "version_conflict", "current_version": exc.current_version}, status=409)
    except SemanticLockViolation as exc:
        return JsonResponse({"error": "semantic_lock", "question_uuid": exc.question_uuid}, status=422)
    return JsonResponse({"definition": revision.definition})


@node_api
@require_POST
def heartbeat(request):
    return JsonResponse({"node_uuid": str(request.node_device.uuid), "server_time": timezone.now().isoformat()})
