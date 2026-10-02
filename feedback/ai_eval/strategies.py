"""Two ways for the model to state numbers.

- grounded (production): the model copies numbers from the evidence; a checker rejects any number it cannot match.
- placeholder: the model writes evidence codes such as {E012}; code fills in the real value, so a
  number can only be wrong if the code is wrong — invention is ruled out by construction.
"""

import copy
import re
from dataclasses import dataclass
from typing import Callable

from .. import ai_synthesis_service
from ..ai_grounding import number_tokens
from ..ai_stage_service import _describe_evidence_aliases, _system_instruction

PROFILE = "standard"
PLACEHOLDER_RE = re.compile(r"\{(E\d{3})(\.n)?\}")
NUMBER_RULE = (
    "可以引用數字，但只能照抄所引用 evidence 的數值、樣本數或標籤中的數字，並依 evidence 精度四捨五入；"
    "不要自行計算差距、比例或目標值。"
)
PLACEHOLDER_RULE = (
    "所有數字一律寫成引用代號，不得直接寫出阿拉伯數字：{E012} 代表 evidence E012 的數值，"
    "{E012.n} 代表它的樣本數；代號必須屬於該項 evidence_refs，後端會換成實際數字。"
    "不要自行計算差距、比例或目標值。"
)


@dataclass(frozen=True)
class Strategy:
    key: str
    label: str
    system: Callable
    schema: Callable
    render: Callable


def _grounded_system(case):
    return _system_instruction(ai_synthesis_service, PROFILE, evidence_aliases_bound=bool(case["aliases"]))


def _grounded_schema(case):
    return _describe_evidence_aliases(ai_synthesis_service.response_schema_for_profile(PROFILE), case["aliases"])


def _placeholder_system(case):
    system = _grounded_system(case)
    if NUMBER_RULE not in system:
        raise RuntimeError("synthesis number rule changed; update the placeholder strategy")
    return system.replace(NUMBER_RULE, PLACEHOLDER_RULE)


def _placeholder_schema(case):
    schema = copy.deepcopy(_grounded_schema(case))

    def visit(node):
        if isinstance(node, dict):
            description = node.get("description")
            if isinstance(description, str) and "只能照抄" in description:
                node["description"] = re.sub(
                    r"數字只能照抄[^。；）]*", "數字一律寫成 evidence 代號（例如 {E001}），不得直接寫阿拉伯數字", description
                )
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(schema)
    return schema


def format_evidence_value(row, field):
    if field == "n":
        size = row.get("sample_size")
        return str(int(size)) if isinstance(size, (int, float)) and not isinstance(size, bool) else None
    value = row.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if row.get("metric_type") == "p_value":
        return "< 0.001" if value < 0.001 else f"{value:.3f}"
    if row.get("kind") == "categorical_distribution" and 0 <= value <= 1:
        return f"{value * 100:.1f}%"
    if float(value).is_integer():
        return str(int(value))
    if abs(value) >= 1:
        return f"{value:.2f}"
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _codes_in(node):
    if isinstance(node, str):
        return [match.group(1) for match in PLACEHOLDER_RE.finditer(node)]
    if isinstance(node, list):
        return [code for item in node for code in _codes_in(item)]
    if isinstance(node, dict):
        return [code for key, value in node.items() if key != "evidence_refs" for code in _codes_in(value)]
    return []


def render_placeholders(payload, registry, *, max_refs=None):
    """Replace {E###} / {E###.n} codes in every text field except evidence_refs.

    A code states exactly where its number comes from, so a code used inside a finding or
    draft is added to that item's evidence_refs (up to the per-item limit). The grounding
    check then sees the same evidence the number was rendered from.
    """

    if max_refs is None:
        max_refs = ai_synthesis_service.PROFILE_LIMITS[PROFILE]["evidence_refs"]
    stats = {"placeholders": 0, "unresolved_placeholders": 0, "raw_digit_tokens": 0, "auto_cited": 0}

    def replace(match):
        stats["placeholders"] += 1
        row = registry.get(match.group(1))
        rendered = format_evidence_value(row, "n" if match.group(2) else "value") if row else None
        if rendered is None:
            stats["unresolved_placeholders"] += 1
            return "?"
        return rendered

    def cite(item):
        refs = item.get("evidence_refs")
        if not isinstance(refs, list):
            return
        for code in dict.fromkeys(_codes_in(item)):
            if code in registry and code not in refs and len(refs) < max_refs:
                refs.append(code)
                stats["auto_cited"] += 1

    def visit(node, key=None):
        if key == "evidence_refs":
            return node
        if isinstance(node, str):
            stats["raw_digit_tokens"] += len(number_tokens(PLACEHOLDER_RE.sub("", node)))
            return PLACEHOLDER_RE.sub(replace, node)
        if isinstance(node, list):
            return [visit(item) for item in node]
        if isinstance(node, dict):
            if "evidence_refs" in node:
                node = {**node, "evidence_refs": list(node["evidence_refs"]) if isinstance(node["evidence_refs"], list)
                        else node["evidence_refs"]}
                cite(node)
            return {name: visit(value, name) for name, value in node.items()}
        return node

    return visit(payload), stats


GROUNDED = Strategy(
    key="grounded",
    label="照抄數字＋事後查核（現行）",
    system=_grounded_system,
    schema=_grounded_schema,
    render=lambda payload, registry: (payload, {}),
)
PLACEHOLDER = Strategy(
    key="placeholder",
    label="引用代號，由程式填數字",
    system=_placeholder_system,
    schema=_placeholder_schema,
    render=render_placeholders,
)
STRATEGIES = {strategy.key: strategy for strategy in (GROUNDED, PLACEHOLDER)}
