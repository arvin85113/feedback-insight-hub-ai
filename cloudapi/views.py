from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from feedback.models import Survey

from .auth import BadRequest, CursorInvalid, make_cursor, node_api, parse_cursor, read_json
from .inbox import ack_items, database_bytes, inbox_summary, quarantine_items, survey_sequences
from .errors import DefinitionError, SemanticLockViolation, VersionConflict
from .models import ChangeClock, InboxSubmission, SurveyChange, SurveyDefinitionRevision
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
    device = request.node_device
    return JsonResponse({
        "node_uuid": str(device.uuid),
        "server_time": timezone.now().isoformat(),
        "inbox": inbox_summary(device),
        "database_bytes": database_bytes(),
        "surveys": survey_sequences(device),
    })


INBOX_PAGE = 100
ACK_BATCH = 200


@node_api
@require_GET
def inbox_list(request):
    try:
        limit = max(1, min(int(request.GET.get("limit", INBOX_PAGE)), INBOX_PAGE))
    except ValueError:
        return JsonResponse({"error": "bad_request", "message": "limit must be an integer"}, status=400)
    rows = list(
        InboxSubmission.objects.filter(node=request.node_device, state=InboxSubmission.State.PENDING)
        .order_by("received_at", "submission_uuid")
        .values_list("envelope", flat=True)[: limit + 1]
    )
    return JsonResponse({"items": rows[:limit], "has_more": len(rows) > limit})


def _items(request, required):
    body = read_json(request)
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list) or len(items) > ACK_BATCH:
        raise BadRequest("items must be a list of at most 200 entries")
    for item in items:
        if not isinstance(item, dict) or any(key not in item for key in required):
            raise BadRequest("each item needs " + ", ".join(required))
    return items


@node_api
@require_POST
def inbox_ack(request):
    items = _items(request, ("submission_uuid", "payload_hash"))
    return JsonResponse({"results": ack_items(request.node_device, items)})


@node_api
@require_POST
def inbox_quarantine(request):
    items = _items(request, ("submission_uuid", "reason"))
    try:
        results = quarantine_items(request.node_device, items)
    except ValueError as exc:
        return JsonResponse({"error": "bad_request", "message": str(exc)}, status=400)
    return JsonResponse({"results": results})
