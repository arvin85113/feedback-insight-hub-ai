"""One dict format for a survey definition, shared by the cloud and the node.

Editing always changes the dict first; `apply_definition` then writes it onto
the models.  `schema_version` 2 carries choice codes, scores and scale ranges
(survey builder spec §4.3); stored `schema_version` 1 revisions are upgraded
with a fixed rule when read, so the cloud and the node derive the same codes.
"""

import copy
import re
import uuid
from datetime import datetime

from django.core.exceptions import ValidationError
from django.db import transaction

from feedback.models import Question, SurveyCategory
from feedback.question_schema import (
    CHOICE_KINDS,
    derive_data_type,
    fields_from_legacy,
    normalize_question,
    question_errors,
)

from .errors import DefinitionError

SCHEMA_VERSION = 2
# Survey settings a published survey may still change (builder spec §4.2); everything else is frozen.
WHITELIST_FIELDS = (
    "is_active",
    "archived_at",
    "category",
    "analysis_enabled",
    "thank_you_email_enabled",
    "improvement_tracking_enabled",
)
SURVEY_FIELDS = (
    "title",
    "slug",
    "description",
    "is_active",
    "analysis_enabled",
    "thank_you_email_enabled",
    "improvement_tracking_enabled",
)
QUESTION_FIELDS = (
    "code",
    "title",
    "help_text",
    "kind",
    "data_type",
    "display",
    "ordered",
    "score_start",
    "scale_min",
    "scale_max",
    "scale_min_label",
    "scale_max_label",
    "choices",
    "next_choice_number",
    "is_required",
    "enable_keyword_tracking",
    "is_active",
    "order",
)
EDITABLE_QUESTION_FIELDS = tuple(field for field in QUESTION_FIELDS if field not in ("code", "is_active"))
EDITABLE_SURVEY_FIELDS = ("title", "description", "is_active", "analysis_enabled", "thank_you_email_enabled")
_Q_CODE_RE = re.compile(r"^q(\d+)$")


def _iso(value):
    return value.isoformat() if value else None


def definition_from_fields(survey, questions, category_name):
    """Plain field reads only (no properties), so migrations can call it with historical models."""

    return {
        "schema_version": SCHEMA_VERSION,
        "survey_uuid": str(survey.uuid),
        "version": survey.definition_version,
        **{field: getattr(survey, field) for field in SURVEY_FIELDS},
        "category": category_name,
        "archived_at": _iso(survey.archived_at),
        "published": survey.published_version is not None,
        "published_version": survey.published_version,
        "published_at": _iso(survey.published_at),
        "analysis_definition_version": survey.analysis_definition_version,
        "next_question_number": survey.next_question_number,
        "questions": [
            {"uuid": str(question.uuid), **{field: copy.deepcopy(getattr(question, field)) for field in QUESTION_FIELDS}}
            for question in questions
        ],
    }


def blank_definition(survey_uuid, *, title="S", description="", category=None, is_active=True,
                     analysis_enabled=True, thank_you_email_enabled=True, improvement_tracking_enabled=True):
    """A new, empty draft definition (version 0) in the current schema."""

    return {
        "schema_version": SCHEMA_VERSION, "survey_uuid": str(survey_uuid), "version": 0, "title": title, "slug": "",
        "description": description, "is_active": is_active, "analysis_enabled": analysis_enabled,
        "thank_you_email_enabled": thank_you_email_enabled,
        "improvement_tracking_enabled": improvement_tracking_enabled, "category": category, "archived_at": None,
        "published": False, "published_version": None, "published_at": None, "analysis_definition_version": None,
        "next_question_number": 1, "questions": [],
    }


def frozen_fields_changed(current, incoming):
    """True when a published survey's definition changes outside the whitelist (spec §4.2)."""

    if not incoming["published"]:
        return True
    if any(current[field] != incoming[field] for field in ("title", "description")):
        return True
    current_questions = {item["uuid"]: item for item in current["questions"]}
    incoming_questions = {str(item["uuid"]): item for item in incoming["questions"]}
    if current_questions.keys() != incoming_questions.keys():
        return True
    return any(
        current_questions[key][field] != incoming_questions[key][field]
        for key in current_questions
        for field in QUESTION_FIELDS
    )


def serialize_definition(survey):
    return definition_from_fields(
        survey,
        survey.questions.order_by("order", "id"),
        survey.category.name if survey.category_id else None,
    )


def upgrade_v1(definition):
    """schema_version 1 → 2 with the fixed rules of spec §4.3; v2 input is returned as a copy."""

    out = copy.deepcopy(definition)
    if out.get("schema_version") == SCHEMA_VERSION:
        return out
    questions = []
    for raw in out.get("questions", []):
        item = dict(raw)
        options_text = item.pop("options_text", "")
        item.update(
            display="", ordered=False, score_start=1, scale_min=None, scale_max=None, scale_min_label="",
            scale_max_label="", choices=[], next_choice_number=1,
        )
        if item.get("kind") in CHOICE_KINDS or item.get("kind") == "scale":
            item.update(fields_from_legacy(item.get("kind"), item.get("data_type"), options_text))
        normalized = normalize_question({**item, "options_text": ""})
        item.update({field: normalized[field] for field in QUESTION_FIELDS if field in normalized})
        questions.append(item)
    numbers = [int(m.group(1)) for m in (_Q_CODE_RE.match(q.get("code") or "") for q in questions) if m]
    version = out.get("version")
    out.update(
        schema_version=SCHEMA_VERSION,
        questions=questions,
        published=True,
        published_version=version,
        published_at=None,
        analysis_definition_version=version,
        next_question_number=max(numbers, default=0) + 1,
    )
    return out


MAX_QUESTIONS = 200
SLUG_RE = re.compile(r"^[-a-zA-Z0-9_]*$")


def _require(condition, message):
    if not condition:
        raise DefinitionError(message)


def _text(value, name, *, max_length, allow_empty=True):
    _require(isinstance(value, str), f"{name} 必須是字串")
    _require(allow_empty or value.strip(), f"{name} 不可空白")
    _require(len(value) <= max_length, f"{name} 超過 {max_length} 字")


def _flag(value, name):
    _require(isinstance(value, bool), f"{name} 必須是布林值")


def _int(value, name, *, allow_none=False, minimum=0):
    if allow_none and value is None:
        return
    _require(isinstance(value, int) and not isinstance(value, bool) and value >= minimum, f"{name} 必須是整數")


def _uuid(value, name):
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise DefinitionError(f"{name} 格式錯誤") from exc


def _datetime(value, name):
    if value is None:
        return
    _require(isinstance(value, str), f"{name} 格式錯誤")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise DefinitionError(f"{name} 格式錯誤") from exc


def _validate_question(item):
    absent = [key for key in ("uuid", *QUESTION_FIELDS) if key not in item]
    _require(not absent, f"題目缺少欄位：{', '.join(absent)}")
    _text(item["code"], "code", max_length=80)
    _text(item["title"], "題目名稱", max_length=255, allow_empty=False)
    _text(item["help_text"], "補充說明", max_length=255)
    for name in ("is_required", "enable_keyword_tracking", "is_active", "ordered"):
        _flag(item[name], name)
    _int(item["order"], "排序")
    _require(item["order"] <= 10000, "排序必須是 0–10000 的整數")
    _int(item["next_choice_number"], "next_choice_number", minimum=1)
    _require(isinstance(item["choices"], list) and all(isinstance(c, dict) for c in item["choices"]), "選項格式錯誤")
    for choice in item["choices"]:
        _text(choice.get("code", ""), "選項代碼", max_length=16)
        _require(isinstance(choice.get("label"), str), "選項文字必須是字串")
    for name in ("scale_min", "scale_max"):
        _int(item[name], name, allow_none=True)
    errors = question_errors(item)
    if errors:
        raise DefinitionError(next(iter(errors.values())))
    _require(item["data_type"] == derive_data_type(item["kind"], ordered=item["ordered"]), "資料型態與作答形式不相容")


def validate_definition(definition):
    """Reject anything that is not a complete, well-typed definition; return the v2 copy to use."""

    _require(isinstance(definition, dict), "定義必須是物件")
    definition = upgrade_v1(definition)
    required = ("survey_uuid", "version", *SURVEY_FIELDS, "category", "archived_at", "questions", "published",
                "published_version", "published_at", "analysis_definition_version", "next_question_number")
    missing = [key for key in required if key not in definition]
    _require(not missing, f"缺少欄位：{', '.join(missing)}")
    _uuid(definition["survey_uuid"], "survey_uuid")
    _int(definition["version"], "version")
    _text(definition["title"], "title", max_length=255, allow_empty=False)
    _text(definition["slug"], "slug", max_length=50)
    _require(SLUG_RE.match(definition["slug"]) is not None, "slug 只能包含英數字、- 與 _")
    _text(definition["description"], "description", max_length=10000)
    for name in ("is_active", "analysis_enabled", "thank_you_email_enabled", "improvement_tracking_enabled", "published"):
        _flag(definition[name], name)
    category = definition["category"]
    _require(category is None or (isinstance(category, str) and 0 < len(category.strip()) <= 100), "category 格式錯誤")
    _datetime(definition["archived_at"], "archived_at")
    _datetime(definition["published_at"], "published_at")
    _int(definition["published_version"], "published_version", allow_none=True)
    _int(definition["analysis_definition_version"], "analysis_definition_version", allow_none=True)
    _int(definition["next_question_number"], "next_question_number", minimum=1)
    questions = definition["questions"]
    _require(isinstance(questions, list), "questions 必須是陣列")
    _require(len(questions) <= MAX_QUESTIONS, f"題目不得超過 {MAX_QUESTIONS} 題")
    seen = set()
    for item in questions:
        _require(isinstance(item, dict), "題目必須是物件")
        key = _uuid(item.get("uuid"), "題目 uuid")
        _require(key not in seen, "題目 uuid 重複")
        seen.add(key)
        _validate_question(item)
    return definition


def _parse_time(value):
    return datetime.fromisoformat(value) if value else None


@transaction.atomic
def apply_definition(survey, definition, *, version):
    """Write the definition onto the models, saving only rows and fields that actually change (spec §7.2)."""

    definition = validate_definition(definition)
    name = definition["category"]
    target = {field: definition[field] for field in SURVEY_FIELDS}
    if survey.pk and not definition["slug"]:
        target.pop("slug")
    target.update(
        category=SurveyCategory.objects.get_or_create(name=name)[0] if name else None,
        archived_at=_parse_time(definition["archived_at"]),
        definition_version=version,
        published_version=definition["published_version"],
        published_at=_parse_time(definition["published_at"]),
        analysis_definition_version=definition["analysis_definition_version"],
        next_question_number=max(definition["next_question_number"], survey.next_question_number or 1),
    )
    changed = [field for field, value in target.items() if getattr(survey, field) != value]
    for field, value in target.items():
        setattr(survey, field, value)
    if survey.pk is None:
        survey.save()
    elif changed:
        survey.save(update_fields=[*changed, "updated_at"] if "updated_at" not in changed else changed)

    existing = {str(question.uuid): question for question in survey.questions.all()}
    seen = set()
    for item in definition["questions"]:
        key = str(item["uuid"])
        question = existing.get(key)
        is_new = question is None
        if is_new:
            question = Question(survey=survey, uuid=key)
        fields_changed = [field for field in QUESTION_FIELDS if getattr(question, field) != item[field]]
        if not is_new and not fields_changed:
            seen.add(key)
            continue
        for field in QUESTION_FIELDS:
            setattr(question, field, copy.deepcopy(item[field]))
        try:
            question.clean()
        except ValidationError as exc:
            raise DefinitionError("; ".join(exc.messages)) from exc
        question.save()
        seen.add(key)
    for key, question in existing.items():
        if key in seen:
            continue
        if survey.published_version is None and not question.answers.exists():
            question.delete()
        elif question.is_active:
            question.is_active = False
            question.save(update_fields=["is_active"])


def _question(definition, question_uuid):
    for item in definition["questions"]:
        if item["uuid"] == str(question_uuid):
            return item
    raise DefinitionError("找不到這一題")


def _merge_legacy_options(item, data):
    """Accept the old `options_text`/`data_type` form fields (until the card builder, plan Task 6)."""

    if "options_text" not in data and "data_type" not in data:
        return
    kind = item.get("kind")
    if kind not in CHOICE_KINDS and kind != "scale":
        return
    legacy = fields_from_legacy(kind, data.get("data_type", item.get("data_type")), data.get("options_text", ""))
    codes = {c["label"]: c["code"] for c in item.get("choices", [])}
    for choice in legacy.get("choices", []):
        choice["code"] = codes.get(choice["label"], "")
    legacy.pop("next_choice_number", None)
    item.update(legacy)


def _normalized(item):
    normalized = normalize_question({**item, "options_text": ""})
    item.update({field: normalized[field] for field in QUESTION_FIELDS if field in normalized})
    return item


def add_question(definition, data):
    item = {
        "uuid": str(uuid.uuid4()), "code": "", "title": "", "help_text": "", "kind": "short_text", "data_type": "text",
        "display": "", "ordered": False, "score_start": 1, "scale_min": None, "scale_max": None,
        "scale_min_label": "", "scale_max_label": "", "choices": [], "next_choice_number": 1,
        "is_required": True, "enable_keyword_tracking": False, "is_active": True,
        "order": len(definition["questions"]) + 1,
    }
    item.update({field: data[field] for field in EDITABLE_QUESTION_FIELDS if field in data})
    item["is_required"] = bool(data.get("is_required", True))
    item["enable_keyword_tracking"] = bool(data.get("enable_keyword_tracking", False))
    item["order"] = int(data.get("order") or item["order"])
    _merge_legacy_options(item, data)
    definition["questions"].append(_normalized(item))
    return item


def update_question(definition, question_uuid, data):
    item = _question(definition, question_uuid)
    for field in EDITABLE_QUESTION_FIELDS:
        if field in data:
            item[field] = data[field]
    _merge_legacy_options(item, data)
    _normalized(item)


def delete_question(definition, question_uuid):
    item = _question(definition, question_uuid)
    definition["questions"].remove(item)


def set_question_active(definition, question_uuid, active):
    _question(definition, question_uuid)["is_active"] = bool(active)


def move_question(definition, question_uuid, direction):
    target = _question(definition, question_uuid)
    ordered = sorted(definition["questions"], key=lambda item: item["order"])
    index = ordered.index(target)
    neighbour = index - 1 if direction == "up" else index + 1
    if 0 <= neighbour < len(ordered):
        ordered[index]["order"], ordered[neighbour]["order"] = ordered[neighbour]["order"], ordered[index]["order"]


def update_survey(definition, data):
    for field in EDITABLE_SURVEY_FIELDS:
        if field in data:
            definition[field] = data[field]
    if "category" in data:
        category = data["category"]
        definition["category"] = getattr(category, "name", category) or None


def archive_survey(definition, when):
    definition["is_active"] = False
    definition["analysis_enabled"] = False
    definition["archived_at"] = when.isoformat()
