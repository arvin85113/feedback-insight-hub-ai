"""Run one model call for one case and strategy, then judge it with the production validators."""

from types import SimpleNamespace

from .. import ai_synthesis_service
from ..ai_grounding import number_tokens
from ..ai_stage_service import _safe_validation_reason, _sanitize_provider_payload
from ..models import SurveyAIAnalysisStage
from .strategies import PROFILE

SYNTHESIS_STAGE = SimpleNamespace(stage_type=SurveyAIAnalysisStage.StageType.SYNTHESIS)
TEXT_FIELDS = ("title", "summary", "rationale", "acceptance_criteria")


def _has_number(item):
    for field in TEXT_FIELDS:
        value = item.get(field)
        texts = value if isinstance(value, list) else [value]
        if any(isinstance(text, str) and number_tokens(text) for text in texts):
            return True
    return False


def run_one(provider, case, strategy, *, repeat):
    import json

    record = {
        "survey": case["survey_slug"],
        "strategy": strategy.key,
        "provider": provider.name,
        "model": getattr(provider, "model", provider.name),
        "repeat": repeat,
    }
    contents = json.dumps(case["provider_input"], ensure_ascii=False, separators=(",", ":"))
    try:
        result = provider.generate(system=strategy.system(case), schema=strategy.schema(case), contents=contents)
    except Exception as exc:  # noqa: BLE001 - a failed call is a measured outcome, not a crash
        record.update(outcome="provider_error", reason=getattr(exc, "reason", None) or type(exc).__name__)
        return record
    record.update(
        latency_ms=result.latency_ms,
        prompt_tokens=result.prompt_tokens,
        output_tokens=result.output_tokens,
        finish_reason=result.finish_reason,
        # Kept only in the local results file so scoring changes can be replayed for free.
        raw_payload=result.payload,
    )
    registry = case["provider_registry"]
    payload, render_stats = strategy.render(result.payload, registry)
    record.update(render_stats)
    sanitized, discarded, discarded_numbers = _sanitize_provider_payload(
        payload, SYNTHESIS_STAGE, registry, ai_synthesis_service, PROFILE
    )
    record.update(
        discarded_items=sum(discarded.values()),
        discarded_reasons=discarded,
        discarded_numbers=discarded_numbers,
    )
    try:
        validated = ai_synthesis_service.validate_output(sanitized, registry, case["input_hash"], profile=PROFILE)
    except ValueError as exc:
        record.update(
            outcome="rejected",
            reason=_safe_validation_reason(exc),
            ungrounded_numbers=list(getattr(exc, "numbers", [])),
        )
        return record
    items = validated["combined_findings"] + validated["improvement_drafts"]
    record.update(
        outcome="published",
        kept_findings=len(validated["combined_findings"]),
        kept_drafts=len(validated["improvement_drafts"]),
        numeric_share=round(sum(_has_number(item) for item in items) / len(items), 3) if items else 0.0,
        summary_has_number=bool(number_tokens(validated["executive_summary"])),
    )
    return record
