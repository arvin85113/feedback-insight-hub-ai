"""Values the builder template renders for each question card (builder spec §3)."""

from .forms import UI_TYPE_LABELS
from .question_schema import ui_type_of

UI_LABELS = dict(UI_TYPE_LABELS)


def _base(position):
    return {
        "uuid": "", "position": position, "ui_type": "", "title": "", "help_text": "", "is_required": True,
        "enable_keyword_tracking": False, "ordered": False, "score_start": 1, "allow_decimal": False,
        "scale_min": 1, "scale_max": 5, "scale_min_label": "", "scale_max_label": "", "rows": [],
        "errors": {}, "non_field_errors": [], "is_open": False, "is_active": True,
    }


def card_from_question(question, position):
    card = _base(position)
    ui_type = ui_type_of(question.kind, question.display)
    card.update(
        uuid=str(question.uuid), ui_type=ui_type, title=question.title, help_text=question.help_text,
        is_required=question.is_required, enable_keyword_tracking=question.enable_keyword_tracking,
        ordered=question.ordered, score_start=question.score_start, allow_decimal=question.kind == "decimal",
        scale_min=question.scale_min if question.scale_min is not None else 1,
        scale_max=question.scale_max if question.scale_max is not None else 5,
        scale_min_label=question.scale_min_label, scale_max_label=question.scale_max_label,
        rows=[{"code": c["code"], "label": c["label"], "excluded": c.get("excluded", False)} for c in question.choices],
        is_active=question.is_active,
    )
    return card


def card_from_form(form, position):
    """A posted card re-rendered with what the manager typed, its errors, and open."""

    data = form.data
    card = _base(position)
    card.update(
        uuid=data.get("question_uuid", ""), ui_type=data.get("ui_type", ""), title=data.get("title", ""),
        help_text=data.get("help_text", ""), is_required=bool(data.get("is_required")),
        enable_keyword_tracking=bool(data.get("enable_keyword_tracking")), ordered=bool(data.get("ordered")),
        score_start=data.get("score_start", 1), allow_decimal=bool(data.get("allow_decimal")),
        scale_min=data.get("scale_min", 1), scale_max=data.get("scale_max", 5),
        scale_min_label=data.get("scale_min_label", ""), scale_max_label=data.get("scale_max_label", ""),
        rows=[{"code": row["code"], "label": row["label"], "excluded": row["excluded"]} for row in form.rows],
        errors={name: list(errors) for name, errors in form.errors.items() if name != "__all__"},
        non_field_errors=list(form.non_field_errors()), is_open=True,
    )
    return card


def build_cards(survey, card_form=None):
    """(cards, new_card): saved questions in order, plus the blank or posted new-question card."""

    questions = list(survey.questions.order_by("order", "id"))
    cards = [card_from_question(question, index) for index, question in enumerate(questions, start=1)]
    new_card = _base(len(cards) + 1)
    if card_form is not None:
        posted = card_form.data.get("question_uuid", "")
        match = next((i for i, card in enumerate(cards) if posted and card["uuid"] == posted), None)
        if match is None:
            new_card = card_from_form(card_form, len(cards) + 1)
        else:
            cards[match] = card_from_form(card_form, match + 1)
    for card in [*cards, new_card]:
        card["type_label"] = UI_LABELS.get(card["ui_type"], "")
    return cards, new_card
