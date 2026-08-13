import json
import math
import re
from collections import Counter

from django.conf import settings


EVIDENCE_PROJECTION_VERSION = "1"
STANDARD_PROFILE = "standard"
COMPACT_PROFILE = "compact"
GENERATION_PROFILES = {STANDARD_PROFILE, COMPACT_PROFILE}
PROMPT_TOKEN_RESERVE = 1500
_SPACE_RE = re.compile(r"\s+")


def effective_prompt_version(prompt_version):
    return f"{prompt_version}-p{EVIDENCE_PROJECTION_VERSION}"


def estimate_input_tokens(value):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return max(1, math.ceil(len(value.encode("utf-8")) / 3))


def profile_budget(profile):
    if profile == COMPACT_PROFILE:
        return {
            "max_evidence_items": settings.AI_REPORT_COMPACT_MAX_EVIDENCE_ITEMS,
            "max_estimated_input_tokens": settings.AI_REPORT_COMPACT_MAX_ESTIMATED_INPUT_TOKENS,
        }
    if profile == STANDARD_PROFILE:
        return {
            "max_evidence_items": settings.AI_REPORT_MAX_EVIDENCE_ITEMS,
            "max_estimated_input_tokens": settings.AI_REPORT_MAX_ESTIMATED_INPUT_TOKENS,
        }
    raise ValueError(f"Unsupported generation profile: {profile}")


def _trim(value, max_length):
    text = _SPACE_RE.sub(" ", str(value or "")).strip()
    return text if len(text) <= max_length else f"{text[: max_length - 3]}..."


def _safe_evidence(row):
    return {
        "id": str(row.get("id") or ""),
        "kind": str(row.get("kind") or "unknown"),
        "label": _trim(row.get("label"), 180),
        "value": row.get("value"),
        "unit": row.get("unit"),
        "sample_size": row.get("sample_size"),
    }


def _numeric_value(row):
    value = row.get("value")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _stable_value_order(row):
    return (-_numeric_value(row), str(row.get("id") or ""))


def _sentiment_group(row):
    evidence_id = str(row.get("id") or "")
    return evidence_id.rsplit(".", 1)[0]


def _significant_test_prefixes(source_snapshot):
    prefixes = []
    for test in source_snapshot.get("statistics", {}).get("statistical_tests", []):
        if test.get("is_significant") and test.get("test_ref"):
            prefixes.append(f"test.{test['test_ref']}.")
    return tuple(sorted(set(prefixes)))


def _improvement_terms(source_snapshot):
    terms = set()
    for item in source_snapshot.get("existing_improvements", []):
        for key in ("related_category", "title"):
            value = _trim(item.get(key), 100).lower()
            if len(value) >= 2:
                terms.add(value)
    return tuple(sorted(terms))


def _ordered_candidates(source_snapshot):
    rows = [row for row in source_snapshot.get("evidence_catalog", []) if row.get("id")]
    by_kind = {}
    for row in rows:
        by_kind.setdefault(str(row.get("kind") or "unknown"), []).append(row)

    groups = []
    groups.append(sorted(by_kind.get("survey_coverage", []) + by_kind.get("analysis_coverage", []), key=lambda row: row["id"]))

    significant_prefixes = _significant_test_prefixes(source_snapshot)
    significant = [
        row
        for row in by_kind.get("statistical_test", [])
        if str(row.get("id") or "").startswith(significant_prefixes)
    ] if significant_prefixes else []
    groups.append(sorted(significant, key=lambda row: row["id"]))

    improvement_terms = _improvement_terms(source_snapshot)
    improvement_related = [
        row
        for row in rows
        if improvement_terms and any(term in str(row.get("label") or "").lower() for term in improvement_terms)
    ]
    groups.append(sorted(improvement_related, key=_stable_value_order))

    sentiment = by_kind.get("category_sentiment", [])
    positive = sorted(
        [row for row in sentiment if str(row.get("id") or "").endswith(".positive")],
        key=_stable_value_order,
    )
    negative = sorted(
        [row for row in sentiment if str(row.get("id") or "").endswith(".negative")],
        key=_stable_value_order,
    )
    groups.append(positive[:1] + negative[:1])

    keywords = sorted(by_kind.get("keyword_frequency", []), key=_stable_value_order)
    groups.append(keywords[:5])

    sentiment_categories = {}
    for row in sorted(sentiment, key=_stable_value_order):
        sentiment_categories.setdefault(_sentiment_group(row), row)
    groups.append(sorted(sentiment_categories.values(), key=_stable_value_order))

    descriptive = sorted(
        by_kind.get("descriptive_statistic", []),
        key=lambda row: (
            0 if str(row.get("id") or "").endswith(".average") else 1,
            str(row.get("id") or ""),
        ),
    )
    groups.append([row for row in descriptive if str(row.get("id") or "").endswith(".average")])
    groups.append(descriptive)
    groups.append(keywords)
    groups.append(sorted(by_kind.get("statistical_test", []), key=lambda row: row["id"]))
    groups.append(sorted(by_kind.get("categorical_distribution", []), key=_stable_value_order))
    groups.append(sorted(rows, key=lambda row: (str(row.get("kind") or ""), str(row.get("id") or ""))))

    ordered = []
    seen = set()
    for group in groups:
        for row in group:
            evidence_id = str(row.get("id") or "")
            if evidence_id and evidence_id not in seen:
                seen.add(evidence_id)
                ordered.append(row)
    return ordered


def _base_model_input(source_snapshot):
    improvements = []
    for item in source_snapshot.get("existing_improvements", [])[:10]:
        improvements.append(
            {
                "ref": item.get("ref"),
                "title": _trim(item.get("title"), 120),
                "summary": _trim(item.get("summary"), 300),
                "related_category": _trim(item.get("related_category"), 80),
                "notification_status": item.get("notification_status"),
            }
        )
    return {
        "schema_version": source_snapshot.get("schema_version"),
        "data_scope": source_snapshot.get("data_scope", {}),
        "dashboard_metrics": source_snapshot.get("dashboard_metrics", {}),
        "response_trend": source_snapshot.get("response_trend", []),
        "existing_improvements": improvements,
        "evidence_catalog": [],
        "data_caveats": [_trim(item, 240) for item in source_snapshot.get("data_caveats", [])[:10]],
    }


def project_evidence(source_snapshot, profile=STANDARD_PROFILE):
    budget = profile_budget(profile)
    full_rows = [row for row in source_snapshot.get("evidence_catalog", []) if row.get("id")]
    ordered = _ordered_candidates(source_snapshot)
    model_input = _base_model_input(source_snapshot)
    selected = []
    excluded_reasons = Counter()

    for row in ordered:
        if len(selected) >= budget["max_evidence_items"]:
            excluded_reasons["item_limit"] += 1
            continue
        safe_row = _safe_evidence(row)
        candidate_input = {**model_input, "evidence_catalog": [*selected, safe_row]}
        if estimate_input_tokens(candidate_input) > max(
            1,
            budget["max_estimated_input_tokens"] - PROMPT_TOKEN_RESERVE,
        ):
            excluded_reasons["token_budget"] += 1
            continue
        selected.append(safe_row)

    selected_ids = {row["id"] for row in selected}
    total_ids = {str(row.get("id")) for row in full_rows}
    unclassified_excluded = len(total_ids - selected_ids) - sum(excluded_reasons.values())
    if unclassified_excluded > 0:
        excluded_reasons["lower_priority"] += unclassified_excluded
    model_input["evidence_catalog"] = selected

    kinds = sorted({str(row.get("kind") or "unknown") for row in full_rows})
    kind_counts = {
        kind: {
            "total": sum(1 for row in full_rows if str(row.get("kind") or "unknown") == kind),
            "selected": sum(1 for row in selected if row["kind"] == kind),
        }
        for kind in kinds
    }
    manifest = {
        "projection_version": EVIDENCE_PROJECTION_VERSION,
        "profile": profile,
        "max_evidence_items": budget["max_evidence_items"],
        "max_estimated_input_tokens": budget["max_estimated_input_tokens"],
        "prompt_token_reserve": PROMPT_TOKEN_RESERVE,
        "total_evidence_count": len(full_rows),
        "selected_evidence_count": len(selected),
        "excluded_evidence_count": len(full_rows) - len(selected),
        "estimated_input_tokens": estimate_input_tokens(model_input),
        "selected_evidence_ids": [row["id"] for row in selected],
        "evidence_kind_counts": kind_counts,
        "excluded_reason_counts": dict(sorted(excluded_reasons.items())),
    }
    return model_input, manifest


def build_projection_manifests(source_snapshot):
    return {
        "projection_version": EVIDENCE_PROJECTION_VERSION,
        "profiles": {
            profile: project_evidence(source_snapshot, profile)[1]
            for profile in (STANDARD_PROFILE, COMPACT_PROFILE)
        },
    }
