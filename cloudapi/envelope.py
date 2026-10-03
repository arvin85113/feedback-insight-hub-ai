"""Inbox envelope and its hashes (spec §2 雜湊, §3 收件封套).

Both sides hash the same canonical JSON: sorted keys, compact separators,
UTF-8 without ASCII escaping, and no string normalisation of any kind, so a
changed space or a different Unicode form is a different answer.
"""

import hashlib
import json
from decimal import Decimal

HASH_VERSION = 1
DEFINITION_HISTORY_RECORDED = "recorded"
# Choice answers are option codes (single: string, multiple: list); builder spec §7.3.
ANSWERS_FORMAT = 2


def canonical_bytes(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(obj):
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def answers_hash(answers):
    return sha256_hex(answers)


def payload_hash(*, survey_uuid, definition_version, consent_follow_up, is_complete, voided_at, answers,
                 answers_format=ANSWERS_FORMAT):
    return sha256_hex({
        "survey_uuid": str(survey_uuid),
        "definition_version": definition_version,
        "consent_follow_up": consent_follow_up,
        "is_complete": is_complete,
        "voided_at": voided_at,
        "answers": answers,
        "answers_format": answers_format,
    })


def envelope_payload_hash(envelope):
    return payload_hash(
        survey_uuid=envelope["survey_uuid"],
        definition_version=envelope["definition_version"],
        consent_follow_up=envelope["consent_follow_up"],
        is_complete=envelope["is_complete"],
        voided_at=envelope["voided_at"],
        answers=envelope["answers"],
        answers_format=envelope.get("answers_format"),
    )


def _encode_value(value):
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def encode_answers(survey, cleaned):
    """Form values keyed by question uuid, keeping JSON types (multiple choice stays a list)."""

    encoded = {}
    for question in survey.questions.filter(is_active=True):
        value = cleaned.get(f"question_{question.id}")
        if value is None or value == "" or value == [] or value == ():
            continue
        encoded[str(question.uuid)] = _encode_value(value)
    return encoded


def answer_text(value):
    """The node's Answer.value: the same string format the existing fill flow stores."""

    if isinstance(value, list):
        return ", ".join(value)
    return str(value)


def build_envelope(*, submission_uuid, survey, definition_version, response_sequence, submitted_at,
                   consent_follow_up, respondent_ref, name, email, answers):
    return {
        "submission_uuid": str(submission_uuid),
        "survey_uuid": str(survey.uuid),
        "definition_version": definition_version,
        "definition_history": DEFINITION_HISTORY_RECORDED,
        "response_sequence": response_sequence,
        "submitted_at": submitted_at.isoformat(),
        "consent_follow_up": consent_follow_up,
        "is_complete": True,
        "voided_at": None,
        "respondent": {"cloud_user_ref": respondent_ref, "name": name, "email": email},
        "answers": answers,
        "answers_format": ANSWERS_FORMAT,
    }
