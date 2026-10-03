import json
from functools import wraps

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .models import NodeDevice


class CursorInvalid(Exception):
    pass


class BadRequest(Exception):
    pass


def make_cursor(device, seq):
    return f"{device.uuid}:{int(seq)}"


def parse_cursor(device, text):
    node_part, _, seq_part = (text or "").partition(":")
    if node_part != str(device.uuid) or not seq_part.isdigit():
        raise CursorInvalid(text)
    return int(seq_part)


def read_json(request):
    try:
        return json.loads(request.body or b"{}")
    except ValueError as exc:
        raise BadRequest("body 不是有效的 JSON") from exc


def node_api(view):
    """Authenticate the node by bearer token; responses never echo answer content."""

    @csrf_exempt
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not settings.CLOUD_SYNC_PROTOTYPE_ENABLED:
            return JsonResponse({"error": "prototype_disabled"}, status=503)
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return JsonResponse({"error": "unauthorized"}, status=401)
        device = NodeDevice.objects.filter(
            token_hash=NodeDevice.hash_token(header[len("Bearer "):].strip()), status=NodeDevice.Status.ACTIVE
        ).first()
        if device is None:
            return JsonResponse({"error": "unauthorized"}, status=401)
        NodeDevice.objects.filter(pk=device.pk).update(last_seen_at=timezone.now())
        request.node_device = device
        try:
            return view(request, *args, **kwargs)
        except BadRequest as exc:
            return JsonResponse({"error": "bad_request", "message": str(exc)}, status=400)

    return wrapped
