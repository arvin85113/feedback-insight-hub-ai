"""One dict format for a survey definition, shared by the cloud and the node.

Editing always changes the dict first; `apply_definition` then writes it onto
the models.  Questions are never deleted here: a question missing from the
definition is deactivated, so answers that point at it survive.
"""

import re
import uuid
from datetime import datetime

from django.core.exceptions import ValidationError
from django.db import transaction

from feedback.models import Question, SurveyCategory

from .errors import DefinitionError

SEMANTIC_FIELDS = ("kind", "data_type", "options_text")
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
    "options_text",
    "is_required",
    "enable_keyword_tracking",
    "is_active",
    "order",
)
EDITABLE_QUESTION_FIELDS = (
    "title",
    "help_text",
    "kind",
    "data_type",
    "options_text",
    "is_required",
    "enable_keyword_tracking",
    "order",
)
EDITABLE_SURVEY_FIELDS = ("title", "description", "is_active", "analysis_enabled", "thank_you_email_enabled")


def serialize_definition(survey):
    questions = survey.questions.order_by("order", "id")
    return {
        "survey_uuid": str(survey.uuid),
        "version": survey.definition_version,
        **{field: getattr(survey, field) for field in SURVEY_FIELDS},
        "category": survey.category.name if survey.category_id else None,
        "archived_at": survey.archived_at.isoformat() if survey.archived_at else None,
        "questions": [
            {"uuid": str(question.uuid), **{field: getattr(question, field) for field in QUESTION_FIELDS}}
            for question in questions
        ],
    }


MAX_QUESTIONS = 200
SLUG_RE = re.compile(r"^[-a-zA-Z0-9_]*$")
# Same compatibility table as Question.clean(), plus a closed set of kinds.
ALLOWED_DATA_TYPES = {
    "short_text": {"text"},
    "long_text": {"text"},
    "single_choice": {"nominal", "ordinal"},
    "multiple_choice": {"nominal"},
    "integer": {"discrete"},
    "decimal": {"continuous"},
    "scale": {"ordinal"},
}


def _require(condition, message):
    if not condition:
        raise DefinitionError(message)


def _text(value, name, *, max_length, allow_empty=True):
    _require(isinstance(value, str), f"{name} 必須是字串")
    _require(allow_empty or value.strip(), f"{name} 不可空白")
    _require(len(value) <= max_length, f"{name} 超過 {max_length} 字")


def _flag(value, name):
    _require(isinstance(value, bool), f"{name} 必須是布林值")


def _uuid(value, name):
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise DefinitionError(f"{name} 格式錯誤") from exc


def validate_definition(definition):
    """Reject anything that is not a complete, well-typed definition. Runs before any database write."""

    _require(isinstance(definition, dict), "定義必須是物件")
    required = ("survey_uuid", "version", *SURVEY_FIELDS, "category", "archived_at", "questions")
    missing = [key for key in required if key not in definition]
    _require(not missing, f"缺少欄位：{', '.join(missing)}")
    _uuid(definition["survey_uuid"], "survey_uuid")
    version = definition["version"]
    _require(isinstance(version, int) and not isinstance(version, bool) and version >= 0, "version 必須是非負整數")
    _text(definition["title"], "title", max_length=255, allow_empty=False)
    _text(definition["slug"], "slug", max_length=50)
    _require(SLUG_RE.match(definition["slug"]) is not None, "slug 只能包含英數字、- 與 _")
    _text(definition["description"], "description", max_length=10000)
    for name in ("is_active", "analysis_enabled", "thank_you_email_enabled", "improvement_tracking_enabled"):
        _flag(definition[name], name)
    category = definition["category"]
    _require(category is None or (isinstance(category, str) and 0 < len(category.strip()) <= 100), "category 格式錯誤")
    archived = definition["archived_at"]
    if archived is not None:
        _require(isinstance(archived, str), "archived_at 格式錯誤")
        try:
            datetime.fromisoformat(archived)
        except ValueError as exc:
            raise DefinitionError("archived_at 格式錯誤") from exc
    questions = definition["questions"]
    _require(isinstance(questions, list), "questions 必須是陣列")
    _require(len(questions) <= MAX_QUESTIONS, f"題目不得超過 {MAX_QUESTIONS} 題")
    seen = set()
    for item in questions:
        _require(isinstance(item, dict), "題目必須是物件")
        absent = [key for key in ("uuid", *QUESTION_FIELDS) if key not in item]
        _require(not absent, f"題目缺少欄位：{', '.join(absent)}")
        key = _uuid(item["uuid"], "題目 uuid")
        _require(key not in seen, "題目 uuid 重複")
        seen.add(key)
        _text(item["code"], "code", max_length=80)
        _text(item["title"], "題目名稱", max_length=255, allow_empty=False)
        _text(item["help_text"], "補充說明", max_length=255)
        _require(item["kind"] in ALLOWED_DATA_TYPES, "作答形式不合法")
        _require(item["data_type"] in ALLOWED_DATA_TYPES[item["kind"]], "資料型態與作答形式不相容")
        _text(item["options_text"], "選項內容", max_length=10000)
        if item["kind"] in {"single_choice", "multiple_choice"}:
            _require(any(line.strip() for line in item["options_text"].splitlines()), "單選與多選題至少需要一個選項")
        for name in ("is_required", "enable_keyword_tracking", "is_active"):
            _flag(item[name], name)
        order = item["order"]
        _require(isinstance(order, int) and not isinstance(order, bool) and 0 <= order <= 10000, "排序必須是 0–10000 的整數")


@transaction.atomic
def apply_definition(survey, definition, *, version):
    validate_definition(definition)
    for field in SURVEY_FIELDS:
        if field == "slug" and survey.pk and not definition["slug"]:
            continue
        setattr(survey, field, definition[field])
    name = definition["category"]
    survey.category = SurveyCategory.objects.get_or_create(name=name)[0] if name else None
    archived = definition["archived_at"]
    survey.archived_at = datetime.fromisoformat(archived) if archived else None
    survey.definition_version = version
    survey.save()

    existing = {str(question.uuid): question for question in survey.questions.all()}
    seen = set()
    for item in definition["questions"]:
        key = str(item["uuid"])
        question = existing.get(key) or Question(survey=survey, uuid=key)
        for field in QUESTION_FIELDS:
            setattr(question, field, item[field])
        try:
            question.clean()
        except ValidationError as exc:
            raise DefinitionError("; ".join(exc.messages)) from exc
        question.save()
        seen.add(key)
    for key, question in existing.items():
        if key not in seen and question.is_active:
            question.is_active = False
            question.save(update_fields=["is_active"])


def _question(definition, question_uuid):
    for item in definition["questions"]:
        if item["uuid"] == str(question_uuid):
            return item
    raise DefinitionError("找不到這一題")


def add_question(definition, data):
    item = {"uuid": str(uuid.uuid4()), "code": "", "is_active": True}
    item.update({field: data.get(field, "") for field in EDITABLE_QUESTION_FIELDS})
    item["is_required"] = bool(data.get("is_required", True))
    item["enable_keyword_tracking"] = bool(data.get("enable_keyword_tracking", False))
    item["order"] = int(data.get("order") or len(definition["questions"]) + 1)
    definition["questions"].append(item)
    return item


def update_question(definition, question_uuid, data):
    item = _question(definition, question_uuid)
    for field in EDITABLE_QUESTION_FIELDS:
        if field in data:
            item[field] = data[field]


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
