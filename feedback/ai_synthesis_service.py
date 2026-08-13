import copy
import hashlib
import json
import re
import uuid
from collections.abc import Mapping


SCHEMA_VERSION = "3"
PROMPT_VERSION = "2"
STAGE_TYPE = "synthesis"
PRIORITIES = {"high", "medium", "low"}
_UNTRUSTED_NUMBER_RE = re.compile(r"\d|百分之[零〇一二兩三四五六七八九十百千萬億]+")

COMBINED_FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "繁體中文綜合發現，不自行撰寫數字。"},
        "source_stages": {
            "type": "array",
            "items": {"type": "string", "enum": ["statistics", "text"]},
            "minItems": 1,
            "maxItems": 2,
        },
        "priority": {"type": "string", "enum": ["high", "medium", "low"]},
        "rationale": {"type": "string", "description": "繁體中文決策依據。"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 4},
        "data_limitations": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
    },
    "required": [
        "title",
        "source_stages",
        "priority",
        "rationale",
        "evidence_refs",
        "data_limitations",
    ],
    "propertyOrdering": [
        "title",
        "source_stages",
        "priority",
        "rationale",
        "evidence_refs",
        "data_limitations",
    ],
}
IMPROVEMENT_DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "改善項目標題。"},
        "summary": {"type": "string", "description": "建議行動內容。"},
        "related_category": {"type": "string", "description": "簡短分類。"},
        "priority": {"type": "string", "enum": ["high", "medium", "low"]},
        "rationale": {"type": "string", "description": "提出此改善的原因。"},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
        "evidence_refs": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 4},
        "data_limitations": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
    },
    "required": [
        "title",
        "summary",
        "related_category",
        "priority",
        "rationale",
        "acceptance_criteria",
        "evidence_refs",
        "data_limitations",
    ],
    "propertyOrdering": [
        "title",
        "summary",
        "related_category",
        "priority",
        "rationale",
        "acceptance_criteria",
        "evidence_refs",
        "data_limitations",
    ],
}
BASE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "executive_summary": {"type": "string", "description": "繁體中文營運摘要，不自行撰寫數字。"},
        "combined_findings": {"type": "array", "items": COMBINED_FINDING_SCHEMA, "maxItems": 5},
        "improvement_drafts": {"type": "array", "items": IMPROVEMENT_DRAFT_SCHEMA, "maxItems": 3},
        "data_caveats": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
    },
    "required": ["executive_summary", "combined_findings", "improvement_drafts", "data_caveats"],
    "propertyOrdering": ["executive_summary", "combined_findings", "improvement_drafts", "data_caveats"],
}

PROFILE_LIMITS = {
    "standard": {
        "findings": 3,
        "drafts": 3,
        "evidence_refs": 3,
        "limitations": 3,
        "caveats": 3,
        "acceptance_criteria": 4,
        "summary_length": 2000,
        "rationale_length": 800,
    },
    "compact": {
        "findings": 2,
        "drafts": 2,
        "evidence_refs": 2,
        "limitations": 2,
        "caveats": 2,
        "acceptance_criteria": 2,
        "summary_length": 1000,
        "rationale_length": 500,
    },
}


def response_schema_for_profile(profile):
    limits = PROFILE_LIMITS[profile]
    schema = copy.deepcopy(BASE_RESPONSE_SCHEMA)
    schema["properties"]["combined_findings"]["maxItems"] = limits["findings"]
    schema["properties"]["improvement_drafts"]["maxItems"] = limits["drafts"]
    schema["properties"]["data_caveats"]["maxItems"] = limits["caveats"]
    for key in ("combined_findings", "improvement_drafts"):
        item = schema["properties"][key]["items"]
        item["properties"]["evidence_refs"]["maxItems"] = limits["evidence_refs"]
        item["properties"]["data_limitations"]["maxItems"] = limits["limitations"]
    draft = schema["properties"]["improvement_drafts"]["items"]
    draft["properties"]["acceptance_criteria"]["maxItems"] = limits["acceptance_criteria"]
    return schema


RESPONSE_SCHEMA = response_schema_for_profile("standard")

SYSTEM_INSTRUCTION = """你是企業營運決策分析師。只能讀取已驗證的統計 stage、文字 stage、匿名改善摘要與資料限制。
不得要求或推測原始 snapshot、Answer.value 或個人資料。不得捏造數字，所有 evidence_refs 必須存在於上游 stage。
survey_slug 與 draft_id 由後端處理，不得輸出。改善草稿只是管理者可編輯的建議，不得聲稱已執行或已通知。
所有自然語言欄位禁止出現 0 到 9；精確數值只由後端依 evidence_refs 顯示。"""


def build_input(statistics_stage, text_stage, improvements, data_scope):
    return {
        "data_scope": data_scope,
        "statistics_analysis": statistics_stage.output_json,
        "text_analysis": text_stage.output_json,
        "existing_improvements": improvements,
        "upstream_stage_ids": {
            "statistics": statistics_stage.pk,
            "text": text_stage.pk,
        },
    }


def _text(value, max_length, reason):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_length
        or _UNTRUSTED_NUMBER_RE.search(value)
    ):
        raise ValueError(reason)
    return value.strip()


def _text_list(value, max_items, max_length, reason):
    if not isinstance(value, list) or len(value) > max_items:
        raise ValueError(reason)
    return [_text(item, max_length, reason) for item in value]


def _refs(value, evidence_by_id, max_items=4):
    if not isinstance(value, list) or not value or len(value) > max_items:
        raise ValueError("invalid_evidence_refs")
    refs = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > 120:
            raise ValueError("invalid_evidence_refs")
        refs.append(item.strip())
    refs = list(dict.fromkeys(refs))
    if not refs or len(refs) > max_items or any(ref not in evidence_by_id for ref in refs):
        raise ValueError("invalid_evidence_refs")
    return refs


def _stable_draft_id(input_hash, index, draft):
    canonical = json.dumps(draft, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"feedback-ai:{input_hash}:{index}:{digest}"))


def validate_output(payload, evidence_by_id, input_hash, *, profile="standard"):
    limits = PROFILE_LIMITS[profile]
    expected = {"executive_summary", "combined_findings", "improvement_drafts", "data_caveats"}
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise ValueError("invalid_synthesis_root")
    result = {
        "executive_summary": _text(
            payload.get("executive_summary"),
            limits["summary_length"],
            "invalid_summary",
        ),
        "combined_findings": [],
        "improvement_drafts": [],
        "data_caveats": _text_list(
            payload.get("data_caveats"),
            limits["caveats"],
            400,
            "invalid_caveats",
        ),
    }
    findings = payload.get("combined_findings")
    if not isinstance(findings, list) or len(findings) > limits["findings"]:
        raise ValueError("invalid_combined_findings")
    finding_keys = {"title", "source_stages", "priority", "rationale", "evidence_refs", "data_limitations"}
    for row in findings:
        if not isinstance(row, Mapping) or set(row) != finding_keys or row.get("priority") not in PRIORITIES:
            raise ValueError("invalid_combined_finding")
        source_stages = row.get("source_stages")
        if not isinstance(source_stages, list) or not source_stages or not set(source_stages) <= {"statistics", "text"}:
            raise ValueError("invalid_source_stages")
        refs = _refs(row.get("evidence_refs"), evidence_by_id, limits["evidence_refs"])
        result["combined_findings"].append(
            {
                "title": _text(row.get("title"), 180, "invalid_finding_title"),
                "source_stages": list(dict.fromkeys(source_stages)),
                "priority": row["priority"],
                "rationale": _text(
                    row.get("rationale"),
                    limits["rationale_length"],
                    "invalid_rationale",
                ),
                "evidence_refs": refs,
                "evidence": [evidence_by_id[ref] for ref in refs],
                "data_limitations": _text_list(
                    row.get("data_limitations"),
                    limits["limitations"],
                    400,
                    "invalid_limitations",
                ),
            }
        )
    drafts = payload.get("improvement_drafts")
    if not isinstance(drafts, list) or len(drafts) > limits["drafts"]:
        raise ValueError("invalid_drafts")
    draft_keys = {
        "title",
        "summary",
        "related_category",
        "priority",
        "rationale",
        "acceptance_criteria",
        "evidence_refs",
        "data_limitations",
    }
    for index, row in enumerate(drafts, start=1):
        if not isinstance(row, Mapping) or set(row) != draft_keys or row.get("priority") not in PRIORITIES:
            raise ValueError("invalid_draft")
        refs = _refs(row.get("evidence_refs"), evidence_by_id, limits["evidence_refs"])
        validated = {
            "title": _text(row.get("title"), 255, "invalid_draft_title"),
            "summary": _text(
                row.get("summary"),
                limits["summary_length"],
                "invalid_draft_summary",
            ),
            "related_category": _text(row.get("related_category"), 100, "invalid_category"),
            "priority": row["priority"],
            "rationale": _text(
                row.get("rationale"),
                limits["rationale_length"],
                "invalid_rationale",
            ),
            "acceptance_criteria": _text_list(
                row.get("acceptance_criteria"),
                limits["acceptance_criteria"],
                300,
                "invalid_acceptance",
            ),
            "evidence_refs": refs,
            "evidence": [evidence_by_id[ref] for ref in refs],
            "data_limitations": _text_list(
                row.get("data_limitations"),
                limits["limitations"],
                400,
                "invalid_limitations",
            ),
        }
        validated["draft_id"] = _stable_draft_id(input_hash, index, validated)
        result["improvement_drafts"].append(validated)
    return result
