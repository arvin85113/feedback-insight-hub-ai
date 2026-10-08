"""Node-side survey edits: the cloud is the only writer (spec §1); the local copy changes only from its reply."""

from cloudapi.errors import DefinitionCommitError, DefinitionError, PublishBlocked, PublishedLocked, VersionConflict

from .client import CLIENT, CONFLICT, SEMANTIC, TRANSIENT, UNAUTHORIZED, CloudError, NotLinked, client_for_link
from .definitions import upsert_definition


class OfflineError(DefinitionCommitError):
    user_message = "離線中，問卷唯讀"


class NotLinkedError(DefinitionCommitError):
    user_message = "尚未連結雲端，問卷唯讀"


class UnauthorizedError(DefinitionCommitError):
    user_message = "雲端連線已撤銷，請重新連結"


def _translate(error):
    if error.kind == TRANSIENT:
        return OfflineError(str(error))
    if error.kind == UNAUTHORIZED:
        return UnauthorizedError(str(error))
    if error.kind == CONFLICT:
        return VersionConflict(error.payload.get("current_version"))
    if error.kind == SEMANTIC:
        if error.payload.get("error") == PublishBlocked.code:
            return PublishBlocked(error.payload.get("message"))
        return PublishedLocked()
    return DefinitionError(str(error))


def _client():
    try:
        return client_for_link()
    except NotLinked as exc:
        raise NotLinkedError() from exc


def node_commit(survey, definition, expected_version):
    client = _client()
    try:
        reply = client.put(f"surveys/{survey.uuid}/", {"expected_version": expected_version, "definition": definition})
    except CloudError as exc:
        raise _translate(exc) from exc
    upsert_definition(reply["definition"])


def create_survey(definition):
    client = _client()
    try:
        reply = client.post("surveys/", definition)
    except CloudError as exc:
        raise _translate(exc) from exc
    return upsert_definition(reply["definition"])[0]
