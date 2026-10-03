"""Question type rules shared by the models, the definition dict and the builder (spec §1, §2).

Everything here works on a plain question "item" dict (the same keys as a
definition question) or on any object with those attributes, so the cloud,
the node, migrations and the builder all derive types and choice codes the
same way.
"""

import re

CHOICE_KINDS = {"single_choice", "multiple_choice"}
TEXT_KINDS = {"short_text", "long_text"}
NUMBER_KINDS = {"integer", "decimal"}
KINDS = CHOICE_KINDS | TEXT_KINDS | NUMBER_KINDS | {"scale"}
DISPLAYS = ("radio", "dropdown")
UI_TYPES = ("short_text", "long_text", "radio", "dropdown", "checkbox", "scale", "number")
SCALE_MIN_CHOICES = (0, 1)
SCALE_MAX_RANGE = range(2, 11)
DEFAULT_SCALE = (1, 5)
MAX_CHOICE_LABEL = 200
MAX_SCALE_LABEL = 40
_CODE_RE = re.compile(r"^c(\d+)$")

ITEM_FIELDS = (
    "kind", "display", "ordered", "score_start", "scale_min", "scale_max", "scale_min_label", "scale_max_label",
    "choices", "next_choice_number", "enable_keyword_tracking", "data_type", "options_text",
)


def derive_data_type(kind, *, ordered):
    if kind in TEXT_KINDS:
        return "text"
    if kind == "single_choice":
        return "ordinal" if ordered else "nominal"
    if kind == "multiple_choice":
        return "nominal"
    if kind == "scale":
        return "ordinal"
    if kind == "integer":
        return "discrete"
    if kind == "decimal":
        return "continuous"
    raise ValueError(f"unknown kind: {kind}")


def ui_type_of(kind, display):
    if kind == "single_choice":
        return display if display in DISPLAYS else "radio"
    if kind == "multiple_choice":
        return "checkbox"
    if kind in NUMBER_KINDS:
        return "number"
    return kind


def kind_display_for(ui_type, *, allow_decimal):
    if ui_type in DISPLAYS:
        return "single_choice", ui_type
    if ui_type == "checkbox":
        return "multiple_choice", ""
    if ui_type == "number":
        return ("decimal" if allow_decimal else "integer"), ""
    if ui_type in ("short_text", "long_text", "scale"):
        return ui_type, ""
    raise ValueError(f"unknown ui type: {ui_type}")


def _code_number(code):
    match = _CODE_RE.match(code or "")
    return int(match.group(1)) if match else 0


def normalize_question(item):
    """Return a new item with codes, scores, data type and mirror fields made consistent."""

    out = dict(item)
    kind = out.get("kind")
    is_choice = kind in CHOICE_KINDS
    out["display"] = (out.get("display") if out.get("display") in DISPLAYS else "radio") if kind == "single_choice" else ""
    out["ordered"] = bool(out.get("ordered")) if kind == "single_choice" else False
    out["score_start"] = out.get("score_start", 1) if out.get("score_start") is not None else 1

    next_number = int(out.get("next_choice_number") or 1)
    choices = []
    if is_choice:
        for raw in out.get("choices") or []:
            label = str(raw.get("label", "")).strip()
            if not label:
                continue
            choices.append({"code": raw.get("code") or "", "label": label,
                            "excluded": bool(raw.get("excluded")), "score": None})
        next_number = max([next_number] + [_code_number(c["code"]) + 1 for c in choices])
        for choice in choices:
            if not choice["code"]:
                choice["code"] = f"c{next_number}"
                next_number += 1
        if out["ordered"]:
            score = out["score_start"]
            for choice in choices:
                if not choice["excluded"]:
                    choice["score"] = score
                    score += 1
    out["choices"] = choices
    out["next_choice_number"] = next_number

    if kind == "scale":
        out["scale_min_label"] = str(out.get("scale_min_label") or "").strip()
        out["scale_max_label"] = str(out.get("scale_max_label") or "").strip()
    else:
        out["scale_min"] = out["scale_max"] = None
        out["scale_min_label"] = out["scale_max_label"] = ""

    out["data_type"] = derive_data_type(kind, ordered=out["ordered"]) if kind in KINDS else out.get("data_type", "")
    out["enable_keyword_tracking"] = bool(out.get("enable_keyword_tracking")) if kind in TEXT_KINDS else False
    if is_choice:
        out["options_text"] = "\n".join(c["label"] for c in choices)
    elif kind == "scale" and out.get("scale_min") is not None and out.get("scale_max") is not None:
        out["options_text"] = "\n".join(str(v) for v in range(out["scale_min"], out["scale_max"] + 1))
    else:
        out["options_text"] = ""
    return out


def question_errors(item):
    errors = {}
    kind = item.get("kind")
    if kind not in KINDS:
        errors["kind"] = "作答形式不合法。"
        return errors
    if item.get("score_start", 1) not in (0, 1):
        errors["score_start"] = "分數只能從 0 或 1 起算。"
    if kind in CHOICE_KINDS:
        choices = [c for c in (item.get("choices") or []) if str(c.get("label", "")).strip()]
        labels = [str(c["label"]).strip() for c in choices]
        included = [c for c in choices if not c.get("excluded")]
        if len(labels) != len(set(labels)):
            errors["choices"] = "選項文字不可重複。"
        elif any(len(label) > MAX_CHOICE_LABEL for label in labels):
            errors["choices"] = f"選項文字不可超過 {MAX_CHOICE_LABEL} 字。"
        elif kind == "multiple_choice" and len(included) != len(choices):
            errors["choices"] = "核取方塊不可設定「不納入分析」。"
        elif kind == "single_choice" and item.get("ordered") and len(included) < 2:
            errors["choices"] = "有高低順序的題目至少需要兩個納入分析的選項。"
        elif not included:
            errors["choices"] = "至少需要一個納入分析的選項。"
        if kind == "single_choice" and item.get("display") not in DISPLAYS:
            errors["display"] = "顯示方式不合法。"
    if kind == "scale":
        if item.get("scale_min") not in SCALE_MIN_CHOICES:
            errors["scale_min"] = "刻度起點只能是 0 或 1。"
        if item.get("scale_max") not in SCALE_MAX_RANGE:
            errors["scale_max"] = "刻度終點必須介於 2 到 10。"
        for name in ("scale_min_label", "scale_max_label"):
            if len(str(item.get(name) or "")) > MAX_SCALE_LABEL:
                errors[name] = f"標籤不可超過 {MAX_SCALE_LABEL} 字。"
    return errors


def _legacy_lines(options_text):
    return [line.strip() for line in (options_text or "").splitlines() if line.strip()]


def fields_from_legacy(kind, data_type, options_text):
    """Old `options_text` + `data_type` → new fields, with the fixed rules of spec §5 step 3."""

    lines = _legacy_lines(options_text)
    if kind == "scale":
        numbers = [int(line) for line in lines if re.fullmatch(r"-?\d+", line)]
        if not lines:
            low, high = DEFAULT_SCALE
            return {"kind": "scale", "scale_min": low, "scale_max": high, "choices": []}
        if len(numbers) == len(lines) and numbers == list(range(numbers[0], numbers[0] + len(numbers))):
            return {"kind": "scale", "scale_min": numbers[0], "scale_max": numbers[-1], "choices": []}
        kind, data_type = "single_choice", "ordinal"
        result = {"kind": kind, "display": "radio"}
    else:
        result = {"kind": kind, "display": "radio" if kind == "single_choice" else ""}
    ordered = kind == "single_choice" and data_type == "ordinal"
    result.update(
        ordered=ordered,
        score_start=1,
        choices=[
            {"code": f"c{index}", "label": label, "excluded": False, "score": index if ordered else None}
            for index, label in enumerate(lines, start=1)
        ],
        next_choice_number=len(lines) + 1,
    )
    return result


def _get(question, name, default=None):
    if isinstance(question, dict):
        return question.get(name, default)
    return getattr(question, name, default)


def analysis_levels(question):
    kind = _get(question, "kind")
    choices = _get(question, "choices") or []
    if kind == "scale":
        low, high = _get(question, "scale_min"), _get(question, "scale_max")
        if low is not None and high is not None:
            return [str(v) for v in range(low, high + 1)]
        return _legacy_lines(_get(question, "options_text"))
    if kind in CHOICE_KINDS:
        if not choices:
            return _legacy_lines(_get(question, "options_text"))
        included = [c for c in choices if not c.get("excluded")]
        if _get(question, "ordered"):
            included = sorted(included, key=lambda c: (c.get("score") is None, c.get("score") or 0))
        return [c["label"] for c in included]
    return []


def analysis_excluded(question):
    if _get(question, "kind") not in CHOICE_KINDS:
        return []
    return [c["label"] for c in (_get(question, "choices") or []) if c.get("excluded")]
