"""Freeze the production synthesis input of a survey so every model sees exactly the same thing."""

from ..ai_stage_service import _canonical_hash, _provider_evidence_projection, build_stage_input
from ..models import SurveyAIAnalysisStage, SurveyAIReportSnapshot, SurveyAnalysisState

SYNTHESIS = SurveyAIAnalysisStage.StageType.SYNTHESIS


def build_case(snapshot):
    """Synthesis input exactly as production sends it (short E-aliases, data scope hidden)."""

    stage_input, evidence_by_id = build_stage_input(snapshot, SYNTHESIS)
    provider_input, provider_registry, alias_to_canonical = _provider_evidence_projection(
        None, stage_input, evidence_by_id
    )
    return {
        "survey_slug": snapshot.survey.slug,
        "survey_title": snapshot.survey.title,
        "snapshot_id": snapshot.pk,
        "input_hash": _canonical_hash(stage_input),
        "aliases": list(alias_to_canonical),
        "provider_input": provider_input,
        "provider_registry": provider_registry,
    }


def snapshot_for(survey):
    """The published AI snapshot, else the newest snapshot whose upstream stages are complete."""

    state = SurveyAnalysisState.objects.filter(survey=survey).select_related("published_ai_stage").first()
    if state and state.published_ai_stage_id:
        return state.published_ai_stage.snapshot
    for snapshot in SurveyAIReportSnapshot.objects.filter(survey=survey).order_by("-pk"):
        try:
            build_stage_input(snapshot, SYNTHESIS)
        except Exception:  # noqa: BLE001 - upstream incomplete; try an older snapshot
            continue
        return snapshot
    return None
