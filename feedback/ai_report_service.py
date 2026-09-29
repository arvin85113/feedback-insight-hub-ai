"""Shared Gemini provider helpers and legacy single-report validation.

Generation runs only through the staged pipeline (ai_stage_service); the
validators here remain for displaying and importing legacy saved reports.
"""

import copy
import re
import threading
import time
from collections.abc import Mapping

from django.conf import settings
from google import genai
from google.genai import types

from .evidence_projection import COMPACT_PROFILE, STANDARD_PROFILE


_REQUEST_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0

REPORT_FIELDS = (
    "executive_summary",
    "positive_signals",
    "critical_findings",
    "statistical_findings",
    "text_insights",
    "improvement_drafts",
    "data_caveats",
)
PROVIDER_REPORT_FIELDS = (
    "executive_summary",
    "findings",
    "improvement_drafts",
    "data_caveats",
)
FINDING_SECTIONS = {
    "positive_signals",
    "critical_findings",
    "statistical_findings",
    "text_insights",
}
PRIORITIES = {"high", "medium", "low"}
_NUMBER_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?%?")
_CHINESE_NUMBER_RE = re.compile(
    r"(?:百分之[零〇一二兩三四五六七八九十百千萬億]+|"
    r"[零〇一二兩三四五六七八九十百千萬億]+(?:%|％|成|倍|份|次|人|項|筆|分|元|天|小時|分鐘|秒))"
)

PROFILE_LIMITS = {
    STANDARD_PROFILE: {
        "findings": 12,
        "findings_per_section": 3,
        "drafts": 3,
        "evidence_refs": 3,
        "limitations": 3,
        "caveats": 5,
        "executive_summary_length": 2500,
        "finding_title_length": 180,
        "rationale_length": 800,
        "draft_title_length": 255,
        "draft_summary_length": 2000,
        "category_length": 100,
        "limitation_length": 400,
    },
    COMPACT_PROFILE: {
        "findings": 8,
        "findings_per_section": 2,
        "drafts": 2,
        "evidence_refs": 2,
        "limitations": 2,
        "caveats": 4,
        "executive_summary_length": 1200,
        "finding_title_length": 120,
        "rationale_length": 400,
        "draft_title_length": 160,
        "draft_summary_length": 1000,
        "category_length": 80,
        "limitation_length": 240,
    },
}

FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "survey_slug": {"type": "string", "description": "來源問卷 slug。"},
        "section": {
            "type": "string",
            "enum": sorted(FINDING_SECTIONS),
            "description": "這項發現所屬的報告區塊。",
        },
        "finding_title": {
            "type": "string",
            "description": "繁體中文標題，不得包含阿拉伯數字；數值由 evidence_refs 顯示。",
        },
        "evidence_refs": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "priority": {"type": "string", "enum": ["high", "medium", "low"]},
        "rationale": {
            "type": "string",
            "description": "繁體中文依據說明，不得包含阿拉伯數字或因果宣稱。",
        },
        "data_limitations": {
            "type": "array",
            "items": {"type": "string", "description": "不得包含阿拉伯數字。"},
        },
    },
    "required": [
        "survey_slug",
        "section",
        "finding_title",
        "evidence_refs",
        "priority",
        "rationale",
        "data_limitations",
    ],
    "propertyOrdering": [
        "survey_slug",
        "section",
        "finding_title",
        "evidence_refs",
        "priority",
        "rationale",
        "data_limitations",
    ],
}

IMPROVEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "改善草稿標題，不得包含阿拉伯數字。"},
        "summary": {"type": "string", "description": "改善草稿內容，不得包含阿拉伯數字。"},
        "related_category": {"type": "string", "description": "簡短分類名稱，不得包含阿拉伯數字。"},
        "priority": {"type": "string", "enum": ["high", "medium", "low"]},
        "rationale": {"type": "string", "description": "改善依據，不得包含阿拉伯數字。"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "data_limitations": {
            "type": "array",
            "items": {"type": "string", "description": "不得包含阿拉伯數字。"},
        },
    },
    "required": [
        "title",
        "summary",
        "related_category",
        "priority",
        "rationale",
        "evidence_refs",
        "data_limitations",
    ],
    "propertyOrdering": [
        "title",
        "summary",
        "related_category",
        "priority",
        "rationale",
        "evidence_refs",
        "data_limitations",
    ],
}

_BASE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "executive_summary": {
            "type": "string",
            "description": "繁體中文營運摘要，不得包含阿拉伯數字；所有數值由後端 evidence 顯示。",
        },
        "findings": {"type": "array", "items": FINDING_SCHEMA},
        "improvement_drafts": {"type": "array", "items": IMPROVEMENT_SCHEMA},
        "data_caveats": {
            "type": "array",
            "items": {"type": "string", "description": "不得包含阿拉伯數字。"},
        },
    },
    "required": list(PROVIDER_REPORT_FIELDS),
    "propertyOrdering": list(PROVIDER_REPORT_FIELDS),
}


def response_schema_for_profile(profile):
    limits = PROFILE_LIMITS[profile]
    schema = copy.deepcopy(_BASE_RESPONSE_SCHEMA)
    schema["properties"]["findings"]["maxItems"] = limits["findings"]
    schema["properties"]["findings"]["items"]["properties"]["evidence_refs"]["maxItems"] = limits[
        "evidence_refs"
    ]
    schema["properties"]["findings"]["items"]["properties"]["data_limitations"]["maxItems"] = limits[
        "limitations"
    ]
    schema["properties"]["improvement_drafts"]["maxItems"] = limits["drafts"]
    schema["properties"]["improvement_drafts"]["items"]["properties"]["evidence_refs"]["maxItems"] = limits[
        "evidence_refs"
    ]
    schema["properties"]["improvement_drafts"]["items"]["properties"]["data_limitations"]["maxItems"] = limits[
        "limitations"
    ]
    schema["properties"]["data_caveats"]["maxItems"] = limits["caveats"]
    return schema


REPORT_RESPONSE_SCHEMA = response_schema_for_profile(STANDARD_PROFILE)

class AIReportError(Exception):
    def __init__(
        self,
        error_code,
        user_message,
        *,
        status_code=502,
        retryable=False,
        reason="",
        http_status=None,
    ):
        super().__init__(error_code)
        self.error_code = error_code
        self.user_message = user_message
        self.status_code = status_code
        self.retryable = retryable
        self.reason = reason
        self.http_status = http_status


def create_gemini_client():
    if not settings.GOOGLE_API_KEY:
        raise AIReportError("not_configured", "AI 服務尚未設定 GOOGLE_API_KEY。", status_code=503)
    return genai.Client(vertexai=True, api_key=settings.GOOGLE_API_KEY)


def _schema_error(reason, message="AI 報告格式無法驗證，請重新產生。"):
    return AIReportError("schema_invalid", message, retryable=True, reason=reason)


def _require_text(value, *, max_length, reason="invalid_text"):
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise _schema_error(reason)
    return value.strip()


def _validate_text_list(value, *, max_items=5, max_length=500, reason="invalid_text_list"):
    if not isinstance(value, list) or len(value) > max_items:
        raise _schema_error(reason)
    return [_require_text(item, max_length=max_length, reason=reason) for item in value]


def _validate_numbers(texts):
    if any(_NUMBER_RE.search(text) or _CHINESE_NUMBER_RE.search(text) for text in texts):
        raise _schema_error("numeric_prose", "AI 報告文字包含未由後端 evidence 顯示的數字。")


def _validate_evidence_refs(value, evidence_by_id, *, max_items):
    refs = _validate_text_list(value, max_items=max_items, max_length=120, reason="invalid_evidence_refs")
    if not refs or len(refs) != len(set(refs)) or any(ref not in evidence_by_id for ref in refs):
        raise _schema_error("invalid_evidence_refs", "AI 報告引用了不存在的分析證據。")
    return refs


def _validate_finding(item, *, survey_slug, evidence_by_id, limits):
    expected = {
        "survey_slug",
        "finding_title",
        "evidence_refs",
        "priority",
        "rationale",
        "data_limitations",
    }
    if not isinstance(item, Mapping) or set(item) != expected or item.get("survey_slug") != survey_slug:
        raise _schema_error("survey_isolation", "AI 報告的問卷範圍不正確。")
    if item.get("priority") not in PRIORITIES:
        raise _schema_error("invalid_priority")
    refs = _validate_evidence_refs(item.get("evidence_refs"), evidence_by_id, max_items=limits["evidence_refs"])
    title = _require_text(item.get("finding_title"), max_length=limits["finding_title_length"])
    rationale = _require_text(item.get("rationale"), max_length=limits["rationale_length"])
    limitations = _validate_text_list(
        item.get("data_limitations"),
        max_items=limits["limitations"],
        max_length=limits["limitation_length"],
    )
    evidence = [evidence_by_id[ref] for ref in refs]
    _validate_numbers([title, rationale, *limitations])
    return {
        "survey_slug": survey_slug,
        "finding_title": title,
        "evidence_refs": refs,
        "evidence": evidence,
        "priority": item["priority"],
        "rationale": rationale,
        "data_limitations": limitations,
    }


def _validate_improvement(item, *, evidence_by_id, draft_index, existing_titles, limits):
    expected = {
        "title",
        "summary",
        "related_category",
        "priority",
        "rationale",
        "evidence_refs",
        "data_limitations",
    }
    if not isinstance(item, Mapping) or set(item) != expected or item.get("priority") not in PRIORITIES:
        raise _schema_error("invalid_improvement", "AI 改善草稿格式無法驗證。")
    refs = _validate_evidence_refs(item.get("evidence_refs"), evidence_by_id, max_items=limits["evidence_refs"])
    title = _require_text(item.get("title"), max_length=limits["draft_title_length"])
    if "".join(title.lower().split()) in existing_titles:
        raise _schema_error("duplicate_improvement", "AI 改善草稿與既有改善項目重複。")
    summary = _require_text(item.get("summary"), max_length=limits["draft_summary_length"])
    category = _require_text(item.get("related_category"), max_length=limits["category_length"])
    rationale = _require_text(item.get("rationale"), max_length=limits["rationale_length"])
    limitations = _validate_text_list(
        item.get("data_limitations"),
        max_items=limits["limitations"],
        max_length=limits["limitation_length"],
    )
    evidence = [evidence_by_id[ref] for ref in refs]
    _validate_numbers([title, summary, category, rationale, *limitations])
    return {
        "draft_id": f"draft-{draft_index}",
        "title": title,
        "summary": summary,
        "related_category": category,
        "priority": item["priority"],
        "rationale": rationale,
        "evidence_refs": refs,
        "evidence": evidence,
        "data_limitations": limitations,
    }


def validate_report_payload(payload, source_snapshot, *, profile=STANDARD_PROFILE, allowed_evidence_ids=None):
    limits = PROFILE_LIMITS[profile]
    if not isinstance(payload, Mapping) or set(payload) != set(PROVIDER_REPORT_FIELDS):
        raise _schema_error("invalid_root")
    survey_slug = source_snapshot.get("data_scope", {}).get("survey_slug")
    allowed = set(allowed_evidence_ids) if allowed_evidence_ids is not None else None
    evidence_by_id = {
        row.get("id"): row
        for row in source_snapshot.get("evidence_catalog") or []
        if isinstance(row, Mapping)
        and isinstance(row.get("id"), str)
        and (allowed is None or row.get("id") in allowed)
    }
    executive_summary = _require_text(
        payload.get("executive_summary"),
        max_length=limits["executive_summary_length"],
        reason="executive_summary_length",
    )
    _validate_numbers([executive_summary])
    validated = {"executive_summary": executive_summary, **{section: [] for section in FINDING_SECTIONS}}
    findings = payload.get("findings")
    if not isinstance(findings, list) or len(findings) > limits["findings"]:
        raise _schema_error("finding_count")
    for item in findings:
        if not isinstance(item, Mapping) or item.get("section") not in FINDING_SECTIONS:
            raise _schema_error("invalid_section")
        section = item["section"]
        if len(validated[section]) >= limits["findings_per_section"]:
            raise _schema_error("section_finding_count")
        validated[section].append(
            _validate_finding(
                {key: value for key, value in item.items() if key != "section"},
                survey_slug=survey_slug,
                evidence_by_id=evidence_by_id,
                limits=limits,
            )
        )
    drafts = payload.get("improvement_drafts")
    if not isinstance(drafts, list) or len(drafts) > limits["drafts"]:
        raise _schema_error("draft_count", "AI 改善草稿格式無法驗證。")
    existing_titles = {
        "".join(str(item.get("title") or "").lower().split())
        for item in source_snapshot.get("existing_improvements", [])
    }
    validated["improvement_drafts"] = [
        _validate_improvement(
            item,
            evidence_by_id=evidence_by_id,
            draft_index=index,
            existing_titles=existing_titles,
            limits=limits,
        )
        for index, item in enumerate(drafts, start=1)
    ]
    validated["data_caveats"] = _validate_text_list(
        payload.get("data_caveats"),
        max_items=limits["caveats"],
        max_length=limits["limitation_length"],
    )
    _validate_numbers(validated["data_caveats"])
    return validated


def _provider_error(exc):
    code = getattr(exc, "code", None)
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    if code == 401:
        return AIReportError(
            "authentication_error",
            "AI API 金鑰驗證失敗，請檢查伺服器設定。",
            status_code=503,
            http_status=code,
        )
    if code == 403:
        return AIReportError("forbidden", "AI API 金鑰沒有執行此模型的權限。", status_code=503, http_status=code)
    if code == 404:
        return AIReportError("model_not_found", "找不到設定的 Gemini 模型。", status_code=503, http_status=code)
    if code == 429:
        return AIReportError(
            "rate_limited",
            "AI 服務請求過多或額度不足，請稍後再試。",
            status_code=429,
            retryable=True,
            http_status=code,
        )
    if code in {408, 504} or "timeout" in type(exc).__name__.lower():
        return AIReportError(
            "timeout",
            "AI 產生報告逾時，既有報告仍可正常使用。",
            status_code=504,
            retryable=True,
            http_status=code,
        )
    if code in {500, 502, 503}:
        return AIReportError(
            "server_unavailable",
            "AI 服務目前無法完成請求，既有報告仍可正常使用。",
            status_code=503,
            retryable=True,
            http_status=code,
        )
    if code == 400:
        return AIReportError(
            "schema_invalid",
            "AI 服務拒絕目前的報告結構。",
            reason="provider_schema_definition",
            http_status=code,
        )
    return AIReportError(
        "provider_error",
        "AI 服務暫時無法使用，既有報告不受影響。",
        http_status=code,
    )


def uses_legacy_thinking_budget(model_name=None):
    """Gemini 2.x uses token budgets; Gemini 3+ uses thinking levels."""

    return (model_name or settings.GEMINI_MODEL).startswith("gemini-2.")


def generation_options(profile):
    """Model-family specific sampling and thinking options shared by all AI calls."""

    compact = profile == COMPACT_PROFILE
    output_tokens = settings.GEMINI_COMPACT_MAX_OUTPUT_TOKENS if compact else settings.GEMINI_MAX_OUTPUT_TOKENS
    if uses_legacy_thinking_budget():
        thinking_budget = settings.GEMINI_COMPACT_THINKING_BUDGET if compact else settings.GEMINI_THINKING_BUDGET
        return {
            "temperature": 0.2 if settings.GEMINI_TEMPERATURE is None else settings.GEMINI_TEMPERATURE,
            "max_output_tokens": output_tokens,
            "thinking_config": types.ThinkingConfig(thinking_budget=thinking_budget),
        }
    level = settings.GEMINI_COMPACT_THINKING_LEVEL if compact else settings.GEMINI_THINKING_LEVEL
    # Gemini 3 is tuned for its default temperature; only override when configured.
    return {
        "temperature": settings.GEMINI_TEMPERATURE,
        "max_output_tokens": output_tokens,
        "thinking_config": types.ThinkingConfig(thinking_level=level.upper()),
    }


def _wait_for_request_slot():
    global _NEXT_REQUEST_AT
    interval = max(0.0, settings.AI_REPORT_REQUEST_INTERVAL_SECONDS)
    if not interval:
        return
    with _REQUEST_RATE_LOCK:
        now = time.monotonic()
        delay = max(0.0, _NEXT_REQUEST_AT - now)
        if delay:
            time.sleep(delay)
            now = time.monotonic()
        _NEXT_REQUEST_AT = now + interval
