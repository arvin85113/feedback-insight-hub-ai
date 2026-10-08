"""Reader-friendly numbers in published AI prose.

The model must copy evidence numbers verbatim (see ai_grounding), so prose carries
raw values such as 0.7723, 22387.0 or 201295.  Pages show them the way a report
would: two decimals at most, no trailing ".0", thousands separators from 10,000
(so years and small counts stay as written).  Only display text changes; stored
outputs and evidence keep their exact values, and grounding accepts the result.
"""

import copy
import re

from .ai_grounding import _NUMBER_TOKEN_RE

_FINDING_TEXT_FIELDS = ("title", "rationale", "summary")
_LIST_TEXT_FIELDS = ("acceptance_criteria", "data_limitations")


def _format_token(token):
    if "," in token:
        return token
    whole, _, fraction = token.partition(".")
    if fraction:
        number = float(token)
        if number == int(number):
            whole, fraction = str(int(number)), ""
        elif len(fraction) > 2:
            rounded = round(number, 2)
            if rounded == 0:
                return token  # p-values and other tiny values keep their precision
            whole, fraction = f"{rounded:.2f}".split(".")
    if len(whole) >= 5:
        whole = f"{int(whole):,}"
    return f"{whole}.{fraction}" if fraction else whole


def humanize_numbers(text):
    if not isinstance(text, str):
        return text
    return _NUMBER_TOKEN_RE.sub(lambda match: _format_token(match.group(0)), text)


def _humanize_item(item):
    if not isinstance(item, dict):
        return item
    for key in _FINDING_TEXT_FIELDS:
        if key in item:
            item[key] = humanize_numbers(item[key])
    for key in _LIST_TEXT_FIELDS:
        if isinstance(item.get(key), list):
            item[key] = [humanize_numbers(value) for value in item[key]]
    return item


def humanize_ai_payload(payload):
    """A display copy of a published AI payload; evidence rows are left exact."""

    if not isinstance(payload, dict) or not payload:
        return payload  # "no AI result" stays falsy for callers
    out = copy.deepcopy(payload)
    out["executive_summary"] = humanize_numbers(out.get("executive_summary", ""))
    for key in ("combined_findings", "improvement_drafts"):
        out[key] = [_humanize_item(item) for item in out.get(key) or []]
    out["data_caveats"] = [humanize_numbers(value) for value in out.get("data_caveats") or []]
    return out
