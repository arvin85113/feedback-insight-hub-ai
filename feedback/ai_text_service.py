import copy
from collections.abc import Mapping


SCHEMA_VERSION = "2"
PROMPT_VERSION = "6"
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
        "title": {"type": "string", "description": "繁體中文洞察標題；數字只能照抄引用 evidence。"},
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
    "standard": {"findings": 4, "evidence_refs": 4, "limitations": 3},
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

SYSTEM_INSTRUCTION = """你是企業問卷文字洞察分析師，只根據提供的匿名文字聚合 evidence 撰寫繁體中文洞察。
不得接收、推測或重建個別回答；關鍵字出現次數不是回覆人數，兩者不得混用；情緒未知不等於中立。
依對營運決策的重要性排序，優先指出差異、異常、極端值與可行動的訊號；不要重述顯而易見的填答分布。rationale 說明這代表什麼、為何重要。data_limitations 只寫與該項發現直接相關的具體限制，沒有就留空陣列。
可以引用數字，但只能照抄所引用 evidence 的數值、樣本數或標籤中的數字，並依 evidence 精度四捨五入；不要自行計算差距、比例或目標值。
某類 evidence 不存在時，該區塊回傳空陣列；不得編造 evidence ID。"""


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
