"""Survey builder actions for synced (node-owned) surveys.

The page edits a definition dict and hands it to `commit`; the cloud commits
with `change_definition`, the node sends it to the cloud API (cloudsync).
Questions are deactivated, never deleted (spec §2).
"""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone

from feedback.forms import QuestionCreateForm, SurveyEditForm
from feedback.models import Question

from .definition import (
    add_question,
    archive_survey,
    move_question,
    serialize_definition,
    set_question_active,
    update_question,
    update_survey,
)
from .errors import DefinitionCommitError
from .writes import change_definition


def cloud_commit(survey, definition, expected_version):
    change_definition(survey.uuid, expected_version=expected_version, definition=definition)


def expected_version_from(request):
    try:
        return int(request.POST.get("definition_version", ""))
    except ValueError:
        return None


def builder_post(view, request, commit):
    survey = view.object
    action = request.POST.get("action")
    tab = "settings" if action == "update-survey" else "questions"
    back = redirect(reverse("feedback:survey-builder", args=[survey.slug]) + f"?tab={tab}")
    expected = expected_version_from(request)
    if expected is None:
        messages.error(request, "版本不一致，請重新載入")
        return back
    definition = serialize_definition(survey)
    question_uuid = request.POST.get("question_uuid", "")
    success = "問卷已更新。"

    if action == "move-question":
        move_question(definition, question_uuid, request.POST.get("direction"))
    elif action == "delete-question":
        set_question_active(definition, question_uuid, False)
        success = "題目已停用；同步問卷不刪除題目，歷史資料保留。"
    elif action == "restore-question":
        set_question_active(definition, question_uuid, True)
        success = "題目已恢復，會重新納入填答與分析。"
    elif action == "edit-question":
        question = get_object_or_404(Question, uuid=question_uuid, survey=survey)
        form = QuestionCreateForm(request.POST, instance=question)
        if not form.is_valid():
            return view.render_to_response(view.get_context_data(question_form=form, object=survey))
        update_question(definition, question_uuid, form.cleaned_data)
        success = "題目已更新。"
    elif action == "update-survey":
        form = SurveyEditForm(request.POST, instance=survey)
        if not form.is_valid():
            return view.render_to_response(view.get_context_data(survey_edit_form=form, object=survey))
        update_survey(definition, form.cleaned_data)
        success = "問卷設定已儲存。"
    else:
        form = QuestionCreateForm(request.POST)
        if not form.is_valid():
            return view.render_to_response(view.get_context_data(question_form=form, object=survey))
        add_question(definition, form.cleaned_data)
        success = "新題目已加入問卷。"

    try:
        commit(survey, definition, expected)
    except DefinitionCommitError as exc:
        messages.error(request, exc.user_message)
    else:
        messages.success(request, success)
    return back


def archive_post(request, survey, commit):
    expected = expected_version_from(request)
    definition = serialize_definition(survey)
    archive_survey(definition, timezone.now())
    try:
        if expected is None:
            raise DefinitionCommitError()
        commit(survey, definition, expected)
    except DefinitionCommitError as exc:
        messages.error(request, exc.user_message if expected is not None else "版本不一致，請重新載入")
        return False
    messages.success(request, f"問卷「{survey.title}」已封存，歷史資料與分析版本均已保留。")
    return True
