"""Shared helpers for seed commands (builder spec §7.1).

Seeds create a survey through the controlled write path (draft, then publish) and
never edit an existing survey's questions: an existing survey must be rebuilt with
`--reset`, which deletes it through `purge_survey`.
"""

import uuid

from django.core.management.base import CommandError

from cloudapi.definition import add_question, blank_definition, serialize_definition
from cloudapi.writes import change_definition, create_survey

from .models import KeywordCategory, Survey
from .survey_purge import PurgeRefused, purge_survey


def text_question(title, *, long=False, tracked=False, required=True):
    return {"title": title, "kind": "long_text" if long else "short_text", "enable_keyword_tracking": tracked,
            "is_required": required}


def choice_question(title, labels, *, multiple=False, ordered=False, excluded=(), display="radio", required=True):
    return {
        "title": title, "kind": "multiple_choice" if multiple else "single_choice",
        "display": "" if multiple else display, "ordered": ordered, "is_required": required,
        "choices": [{"code": "", "label": label, "excluded": label in excluded} for label in labels],
    }


def scale_question(title, low, high, *, low_label="", high_label="", required=True):
    return {"title": title, "kind": "scale", "scale_min": low, "scale_max": high, "scale_min_label": low_label,
            "scale_max_label": high_label, "is_required": required}


def number_question(title, *, decimal=False, required=True):
    return {"title": title, "kind": "decimal" if decimal else "integer", "is_required": required}


def reset_survey(slug):
    survey = Survey.objects.filter(slug=slug).first()
    if survey is None:
        return
    try:
        purge_survey(survey)
    except PurgeRefused as exc:
        raise CommandError(str(exc)) from exc


def survey_definition(*, survey_uuid, title, questions, description="", category=None,
                      thank_you_email_enabled=False):
    definition = blank_definition(survey_uuid, title=title, description=description, category=category,
                                  thank_you_email_enabled=thank_you_email_enabled)
    for order, question in enumerate(questions, start=1):
        add_question(definition, {**question, "order": order})
    return definition


def _create_keywords(survey, keywords):
    for keyword, category_name in keywords:
        KeywordCategory.objects.create(survey=survey, keyword=keyword, category=category_name, threshold=2)


def create_published_survey(*, slug, title, questions, description="", category=None, keywords=(),
                            thank_you_email_enabled=False):
    """Create a draft with a fixed slug, add its questions, then publish it. Refuses an existing slug."""

    if Survey.objects.filter(slug=slug).exists():
        raise CommandError(f"問卷 {slug!r} 已存在；要重建請加 --reset")
    definition = survey_definition(survey_uuid=uuid.uuid4(), title=title, questions=questions, description=description,
                                   category=category, thank_you_email_enabled=thank_you_email_enabled)
    survey = create_survey(definition, slug=slug).survey
    published = serialize_definition(survey)
    published["published"] = True
    change_definition(survey.uuid, expected_version=survey.definition_version, definition=published)
    _create_keywords(survey, keywords)
    return Survey.objects.get(pk=survey.pk)


def create_published_node_survey(*, survey_uuid, title, questions, description="", category=None, keywords=()):
    """Node mode: create and publish through the cloud API (the cloud stays the only definition writer).

    Keywords are node-local data (cloud sync spec §1), created only after the publish succeeded.
    """

    from cloudapi.errors import DefinitionCommitError, DefinitionError

    from .survey_lifecycle import commit, create_draft

    if Survey.objects.filter(uuid=survey_uuid).exists():
        raise CommandError(f"問卷 {survey_uuid} 已存在於本機；請在節點主控台處理後再執行")
    definition = survey_definition(survey_uuid=survey_uuid, title=title, questions=questions,
                                   description=description, category=category)
    try:
        survey = create_draft(definition)
        published = serialize_definition(survey)
        published["published"] = True
        commit(survey, published, survey.definition_version)
    except (DefinitionCommitError, DefinitionError) as exc:
        raise CommandError(getattr(exc, "user_message", "") or str(exc)) from exc
    survey = Survey.objects.get(uuid=survey_uuid)
    _create_keywords(survey, keywords)
    return survey


def inbox_answers(survey, values):
    """{question title: value} → the envelope answers a real fill of the published form would send.

    Labels become choice codes, then the values go through the fill page's own form and encoder.
    """

    from django.utils.datastructures import MultiValueDict

    from cloudapi.envelope import encode_answers

    from .forms import SurveyFormBuilder
    from .local_service import _encode_answer
    from .models import Question

    questions = {question.title: question for question in survey.questions.all()}
    data = MultiValueDict()
    for title, value in values.items():
        if value is None:
            continue
        question = questions[title]
        if question.kind in (Question.Kind.SINGLE_CHOICE, Question.Kind.MULTIPLE_CHOICE):
            data.setlist(f"question_{question.id}", _encode_answer(question, value)[0])
        else:
            data[f"question_{question.id}"] = str(value)
    form = SurveyFormBuilder(data, survey=survey)
    if not form.is_valid():
        raise CommandError(f"模擬答案不符合問卷：{form.errors.as_text()}")
    return encode_answers(survey, form.cleaned_data)


def answers_by_title(survey, values):
    """{question title: value} → the `answers` dict submit_survey_payload expects (labels are accepted)."""

    questions = {question.title: question for question in survey.questions.all()}
    return {f"question_{questions[title].id}": value for title, value in values.items() if value is not None}
