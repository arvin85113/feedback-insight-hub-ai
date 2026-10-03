"""Convert existing survey definitions to the builder schema (survey builder spec §5 steps 3–4).

Runs inside migration 0024 with historical models, so it reads and writes
plain fields only and never calls model methods or sends model signals.  All
guards are checked before anything is written: a survey that cannot be
converted safely stops the whole conversion.
"""

import re

from cloudapi.definition import definition_from_fields

from .question_schema import CHOICE_KINDS, ITEM_FIELDS, fields_from_legacy, normalize_question, question_errors

_Q_CODE_RE = re.compile(r"^q(\d+)$")
_INTEGER_RE = re.compile(r"^-?\d+$")


class ConversionAborted(Exception):
    pass


def _lines(text):
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _is_integer_range(lines):
    if not lines or not all(_INTEGER_RE.match(line) for line in lines):
        return False
    numbers = [int(line) for line in lines]
    return numbers == list(range(numbers[0], numbers[0] + len(numbers)))


def converted_item(question):
    """The question's fields after conversion (spec §5 step 3); shared by the migration and its report."""

    item = {name: getattr(question, name) for name in ITEM_FIELDS}
    legacy_needed = (
        (question.kind in CHOICE_KINDS and not question.choices)
        or (question.kind == "scale" and question.scale_min is None)
    )
    if legacy_needed:
        item.update(fields_from_legacy(question.kind, question.data_type, question.options_text))
    return normalize_question(item)


def _problems(Question, Answer, using):
    problems = []
    # Every converted question must be one the builder accepts, or later edits and syncs would fail.
    for question in Question.objects.using(using).select_related("survey"):
        errors = question_errors(converted_item(question))
        if errors:
            problems.append(f"{question.survey.slug}：{question.title}（轉換後不合法：{next(iter(errors.values()))}）")
    for question in Question.objects.using(using).select_related("survey").filter(
        kind__in=("single_choice", "multiple_choice", "scale")
    ):
        if question.kind in CHOICE_KINDS and question.choices:
            continue
        if question.kind == "scale" and question.scale_min is not None:
            continue
        answers = Answer.objects.using(using).filter(question=question)
        if not answers.exists():
            continue
        lines = _lines(question.options_text)
        reason = None
        if question.kind == "multiple_choice":
            reason = "複選題已有答案"
        elif question.kind == "single_choice":
            if answers.exclude(value__in=lines).exists():
                reason = "單選題答案對不到唯一選項"
        elif lines and not _is_integer_range(lines):
            reason = "文字選項的刻度題已有答案"
        if reason:
            problems.append(f"{question.survey.slug}：{question.title}（{reason}）")
    return problems


def convert_definitions(apps, *, using):
    Survey = apps.get_model("feedback", "Survey")
    Question = apps.get_model("feedback", "Question")
    Answer = apps.get_model("feedback", "Answer")
    Revision = apps.get_model("cloudapi", "SurveyDefinitionRevision")

    problems = _problems(Question, Answer, using)
    if problems:
        raise ConversionAborted("無法轉換，請先清除下列問卷：" + "；".join(problems))

    summary = {"questions": 0, "answers": 0, "surveys": 0, "revisions": 0}
    for question in Question.objects.using(using).all():
        normalized = converted_item(question)
        changed = {name: value for name, value in normalized.items() if getattr(question, name) != value}
        if changed:
            Question.objects.using(using).filter(pk=question.pk).update(**changed)
            summary["questions"] += 1
        if normalized["kind"] == "single_choice":
            codes = {choice["label"]: choice["code"] for choice in normalized["choices"]}
            for answer in Answer.objects.using(using).filter(question_id=question.pk, choice_codes__isnull=True):
                Answer.objects.using(using).filter(pk=answer.pk).update(choice_codes=[codes[answer.value.strip()]])
                summary["answers"] += 1

    for survey in Survey.objects.using(using).filter(published_version__isnull=True):
        codes = Question.objects.using(using).filter(survey_id=survey.pk).values_list("code", flat=True)
        numbers = [int(match.group(1)) for match in (_Q_CODE_RE.match(code or "") for code in codes) if match]
        version = survey.definition_version or 1
        Survey.objects.using(using).filter(pk=survey.pk).update(
            definition_version=version,
            published_version=version,
            analysis_definition_version=version,
            published_at=survey.created_at,
            next_question_number=max(numbers, default=0) + 1,
        )
        summary["surveys"] += 1

    for survey in Survey.objects.using(using).select_related("category").all():
        if Revision.objects.using(using).filter(survey_id=survey.pk, version=survey.definition_version).exists():
            continue
        questions = Question.objects.using(using).filter(survey_id=survey.pk).order_by("order", "id")
        category = survey.category.name if survey.category_id else None
        Revision.objects.using(using).create(
            survey_id=survey.pk,
            version=survey.definition_version,
            definition=definition_from_fields(survey, questions, category),
        )
        summary["revisions"] += 1
    return summary
