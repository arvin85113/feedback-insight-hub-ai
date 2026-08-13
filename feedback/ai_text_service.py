import copy
from collections.abc import Mapping


SCHEMA_VERSION = "2"
PROMPT_VERSION = "2"
STAGE_TYPE = "text"
SECTIONS = (
    "keyword_findings",
    "category_sentiments",
    "positive_signals",
    "negative_signals",
    "text_coverage",
    "text_caveats",
)
TEXT_EVIDENCE_KINDS = {
    "survey_coverage",
    "analysis_coverage",
    "keyword_frequency",
    "category_sentiment",
}

FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "繁體中文洞察標題，不自行撰寫數字。"},
        "rationale": {"type": "string", "description": "繁體中文聚合洞察說明。"},
        "evidence_refs": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 3,
        },
        "data_limitations": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 3,
        },
    },
    "required": ["title", "rationale", "evidence_refs", "data_limitations"],
    "propertyOrdering": ["title", "rationale", "evidence_refs", "data_limitations"],
}
BASE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        section: {"type": "array", "items": FINDING_SCHEMA, "maxItems": 4}
        for section in SECTIONS
    },
    "required": list(SECTIONS),
    "propertyOrdering": list(SECTIONS),
}

PROFILE_LIMITS = {
    "standard": {"findings": 3, "evidence_refs": 3, "limitations": 3},
    "compact": {"findings": 2, "evidence_refs": 2, "limitations": 2},
}


def response_schema_for_profile(profile):
    limits = PROFILE_LIMITS[profile]
    schema = copy.deepcopy(BASE_RESPONSE_SCHEMA)
    for section in SECTIONS:
        section_schema = schema["properties"][section]
        section_schema["maxItems"] = limits["findings"]
        finding = section_schema["items"]
        finding["properties"]["evidence_refs"]["maxItems"] = limits["evidence_refs"]
        finding["properties"]["data_limitations"]["maxItems"] = limits["limitations"]
    return schema


RESPONSE_SCHEMA = response_schema_for_profile("standard")

SYSTEM_INSTRUCTION = """你是企業問卷文字洞察分析師。只能分析提供的匿名文字聚合資料，使用繁體中文。
不得接收、推測或重建 Answer.value。關鍵字出現次數不是 distinct response count，不得混用。
每項洞察只能引用輸入中的 evidence ID；精確數值由後端 evidence 顯示。
title、rationale、data_limitations 等所有文字欄位禁止出現 0 到 9；不要複製帶數字的問卷名稱或自行撰寫數值。"""


def build_input(source_snapshot):
    evidence = [
        dict(row)
        for row in source_snapshot.get("evidence_catalog", [])
        if row.get("kind") in TEXT_EVIDENCE_KINDS
    ]
    return {
        "data_scope": {
            key: source_snapshot.get("data_scope", {}).get(key)
            for key in (
                "survey_slug",
                "survey_title",
                "valid_response_count",
                "source_latest_date",
                "analysis_coverage",
                "text_analysis_version",
            )
        },
        "text_analysis": source_snapshot.get("text_analysis", {}),
        "evidence_catalog": evidence,
        "text_caveats": list(source_snapshot.get("data_caveats", [])),
    }


def validate_output(payload, evidence_by_id, validate_finding, *, profile="standard"):
    limits = PROFILE_LIMITS[profile]
    if not isinstance(payload, Mapping) or set(payload) != set(SECTIONS):
        raise ValueError("invalid_text_root")
    result = {}
    for section in SECTIONS:
        rows = payload.get(section)
        if not isinstance(rows, list) or len(rows) > limits["findings"]:
            raise ValueError("invalid_text_count")
        result[section] = [
            validate_finding(
                row,
                evidence_by_id,
                max_refs=limits["evidence_refs"],
                max_limitations=limits["limitations"],
            )
            for row in rows
        ]
    return result
