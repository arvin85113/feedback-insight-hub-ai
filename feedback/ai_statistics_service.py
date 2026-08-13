import copy
from collections.abc import Mapping


SCHEMA_VERSION = "2"
PROMPT_VERSION = "2"
STAGE_TYPE = "statistics"
SECTIONS = (
    "descriptive_statistics",
    "categorical_distributions",
    "group_comparisons",
    "correlations",
    "inferential_tests",
    "statistical_caveats",
)
STATISTICAL_EVIDENCE_KINDS = {
    "survey_coverage",
    "descriptive_statistic",
    "categorical_distribution",
    "statistical_test",
}

FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "繁體中文發現標題，不自行撰寫數字。"},
        "rationale": {"type": "string", "description": "繁體中文說明，不宣稱因果。"},
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
        section: {
            "type": "array",
            "items": FINDING_SCHEMA,
            "maxItems": 4 if section != "statistical_caveats" else 5,
        }
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

SYSTEM_INSTRUCTION = """你是企業問卷統計分析師。只能分析提供的匿名聚合統計資料，使用繁體中文。
不得讀取或推測個人回答，不得捏造數字或因果。每項發現只能引用輸入中的 evidence ID。
實際變項、檢定方法、統計量、p value、效果量、相關係數與樣本數由後端 evidence 顯示。
title、rationale、data_limitations 等所有文字欄位禁止出現 0 到 9；不要複製帶數字的問卷名稱或自行改寫數值。"""


def build_input(source_snapshot):
    evidence = [
        dict(row)
        for row in source_snapshot.get("evidence_catalog", [])
        if row.get("kind") in STATISTICAL_EVIDENCE_KINDS
    ]
    return {
        "data_scope": {
            key: source_snapshot.get("data_scope", {}).get(key)
            for key in ("survey_slug", "survey_title", "valid_response_count", "source_latest_date", "statistics_version")
        },
        "statistics": source_snapshot.get("statistics", {}),
        "evidence_catalog": evidence,
        "statistical_caveats": list(source_snapshot.get("data_caveats", [])),
    }


def validate_output(payload, evidence_by_id, validate_finding, *, profile="standard"):
    limits = PROFILE_LIMITS[profile]
    if not isinstance(payload, Mapping) or set(payload) != set(SECTIONS):
        raise ValueError("invalid_statistics_root")
    result = {}
    for section in SECTIONS:
        rows = payload.get(section)
        if not isinstance(rows, list) or len(rows) > limits["findings"]:
            raise ValueError("invalid_statistics_count")
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
