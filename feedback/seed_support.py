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


def create_published_survey(*, slug, title, questions, description="", category=None, keywords=(),
                            thank_you_email_enabled=False):
    """Create a draft with a fixed slug, add its questions, then publish it. Refuses an existing slug."""

    if Survey.objects.filter(slug=slug).exists():
        raise CommandError(f"問卷 {slug!r} 已存在；要重建請加 --reset")
    definition = blank_definition(uuid.uuid4(), title=title, description=description, category=category,
                                  thank_you_email_enabled=thank_you_email_enabled)
    for order, question in enumerate(questions, start=1):
        add_question(definition, {**question, "order": order})
    survey = create_survey(definition, slug=slug).survey
    published = serialize_definition(survey)
    published["published"] = True
    change_definition(survey.uuid, expected_version=survey.definition_version, definition=published)
    for keyword, category_name in keywords:
        KeywordCategory.objects.create(survey=survey, keyword=keyword, category=category_name, threshold=2)
    return Survey.objects.get(pk=survey.pk)


def answers_by_title(survey, values):
    """{question title: value} → the `answers` dict submit_survey_payload expects (labels are accepted)."""

    questions = {question.title: question for question in survey.questions.all()}
    return {f"question_{questions[title].id}": value for title, value in values.items() if value is not None}
