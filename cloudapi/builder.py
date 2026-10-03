"""Survey builder actions (builder spec §3, §7.1).

The page edits a definition dict and hands it to `commit` (survey_lifecycle:
the cloud's change_definition, or the cloud API from a node).  Card errors and
version conflicts re-render the page with the posted card kept.
"""

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse

from feedback.forms import QuestionCardForm, SurveyEditForm

from .definition import (
    add_question,
    delete_question,
    duplicate_question,
    move_question,
    serialize_definition,
    update_question,
    update_survey,
)
from .errors import DefinitionCommitError, VersionConflict
from .writes import change_definition

CONFLICT_MESSAGE = "此問卷已在其他視窗修改"


def cloud_commit(survey, definition, expected_version):
    change_definition(survey.uuid, expected_version=expected_version, definition=definition)


def expected_version_from(request):
    try:
        return int(request.POST.get("definition_version", ""))
    except ValueError:
        return None


def _position(definition, question_uuid):
    ordered = sorted(definition["questions"], key=lambda item: item["order"])
    return next(index for index, item in enumerate(ordered, start=1) if item["uuid"] == question_uuid)


def _render_card(view, survey, form, *, conflict=False, error=None):
    context = view.get_context_data(object=survey, card_form=form, card_conflict=conflict, card_error=error)
    return view.render_to_response(context, status=409 if conflict else 200)


def _save_card(view, request, survey, definition, expected, commit):
    form = QuestionCardForm(request.POST)
    if not form.is_valid():
        return _render_card(view, survey, form)
    item = form.to_item()
    question_uuid = form.cleaned_data["question_uuid"]
    if question_uuid:
        update_question(definition, question_uuid, item)
    else:
        add_question(definition, {**item, "order": len(definition["questions"]) + 1})
    try:
        commit(survey, definition, expected)
    except VersionConflict:
        return _render_card(view, survey, form, conflict=True)
    except DefinitionCommitError as exc:
        return _render_card(view, survey, form, error=exc.user_message)
    messages.success(request, "題目已儲存。")
    return None


def builder_post(view, request, commit):
    from feedback.survey_lifecycle import copy_as_draft

    survey = view.object
    action = request.POST.get("action")
    tab = "settings" if action == "update-survey" else "questions"
    back = redirect(reverse("feedback:survey-builder", args=[survey.slug]) + f"?tab={tab}")
    expected = expected_version_from(request)
    if expected is None:
        messages.error(request, "版本不一致，請重新載入")
        return back

    if action == "delete-survey":
        delete_post(request, survey)
        return redirect("feedback:survey-manager")
    if action == "copy":
        try:
            copy = copy_as_draft(survey)
        except DefinitionCommitError as exc:
            messages.error(request, exc.user_message)
            return back
        messages.success(request, "已複製為新草稿。")
        return redirect("feedback:survey-builder", slug=copy.slug)

    definition = serialize_definition(survey)
    question_uuid = request.POST.get("question_uuid", "")
    success = "問卷已更新。"
    if action == "save-question":
        return _save_card(view, request, survey, definition, expected, commit) or back
    if action == "publish":
        definition["published"] = True
        success = "問卷已發布；題目從此固定，需要修改時請複製為新草稿。"
    elif action == "move-question":
        move_question(definition, question_uuid, request.POST.get("direction"))
        success = f"已移到第 {_position(definition, question_uuid)} 題"
    elif action == "duplicate-question":
        duplicate_question(definition, question_uuid)
        success = "題目已複製。"
    elif action == "delete-question":
        delete_question(definition, question_uuid)
        success = "題目已刪除。"
    elif action == "update-survey":
        form = SurveyEditForm(request.POST, instance=survey)
        if not form.is_valid():
            return view.render_to_response(view.get_context_data(survey_edit_form=form, object=survey))
        data = dict(form.cleaned_data)
        if survey.published_version is not None:
            # Title and description are frozen once published (spec §4.2).
            data.pop("title", None)
            data.pop("description", None)
        update_survey(definition, data)
        success = "問卷設定已儲存。"
    else:
        messages.error(request, "不支援的操作")
        return back

    try:
        commit(survey, definition, expected)
    except VersionConflict:
        messages.error(request, CONFLICT_MESSAGE + "，請重新載入")
    except DefinitionCommitError as exc:
        messages.error(request, exc.user_message)
    else:
        messages.success(request, success)
    return back


def delete_post(request, survey):
    """Delete an unassigned draft, otherwise archive (builder spec §5.1, §7.1)."""

    from feedback.survey_lifecycle import delete_or_archive

    expected = expected_version_from(request)
    if expected is None:
        messages.error(request, "版本不一致，請重新載入")
        return False
    try:
        outcome = delete_or_archive(survey, expected)
    except DefinitionCommitError as exc:
        messages.error(request, exc.user_message)
        return False
    if outcome == "deleted":
        messages.success(request, f"草稿「{survey.title}」已刪除。")
    else:
        messages.success(request, f"問卷「{survey.title}」已封存，歷史資料與分析版本均已保留。")
    return True
