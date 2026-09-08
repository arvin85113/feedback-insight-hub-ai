"""Finite, read-only payloads for Render analysis pages."""

from __future__ import annotations

from django.conf import settings
from django.urls import reverse

from .analysis_sources import AnalysisSourceConfigurationError, resolve_analysis_source
from .models import AnalysisJob, ImprovementUpdate, SurveyAnalysisState


def _stage_is_current(state, manifest_key, *, binding=None):
    item = (state.publication_manifest or {}).get(manifest_key) or {}
    if not item:
        return False
    if binding is False:
        return False
    if binding is None:
        try:
            binding = resolve_analysis_source(state.survey_id)
        except AnalysisSourceConfigurationError:
            return False
    return (
        item.get("input_version") == state.input_version
        and item.get("config_version") == state.config_version
        and item.get("pipeline_version") == state.pipeline_version
        and item.get("source_kind", AnalysisJob.SourceKind.ANSWERS) == binding.kind
        and item.get("source_ref", "") == binding.source_ref
        and item.get("source_version", "") == binding.source_version
    )


def is_published_ai_stage_current(stage):
    """Check a published stage from authoritative pointers without scanning answers."""

    state = (
        SurveyAnalysisState.objects.select_related("survey__analysis_source__active_external_version").only(
            "survey",
            "input_version",
            "config_version",
            "pipeline_version",
            "published_snapshot_id",
            "published_ai_stage_id",
            "publication_manifest",
        )
        .filter(survey_id=stage.snapshot.survey_id)
        .first()
    )
    if state is None or state.published_snapshot_id != stage.snapshot_id or state.published_ai_stage_id != stage.pk:
        return False
    try:
        binding = resolve_analysis_source(state.survey)
    except AnalysisSourceConfigurationError:
        return False
    manifest = (state.publication_manifest or {}).get("ai") or {}
    return bool(
        _stage_is_current(state, "ai", binding=binding)
        and manifest.get("snapshot_id") == stage.snapshot_id
        and manifest.get("stage_id") == stage.pk
    )


def _published_draft_states(survey, ai, *, current_ai):
    """Build at most ten manager action links from the bounded published payload."""

    if not current_ai or not isinstance(ai, dict):
        return {}
    stage_id = ai.get("stage_id")
    draft_ids = [
        str(row.get("draft_id"))
        for row in list(ai.get("improvement_drafts") or [])[:10]
        if isinstance(row, dict) and row.get("draft_id")
    ]
    if not stage_id or not draft_ids:
        return {}
    imported = {
        row.source_ai_draft_id: row.pk
        for row in ImprovementUpdate.objects.filter(
            source_ai_analysis_stage_id=stage_id,
            source_ai_draft_id__in=draft_ids,
        ).only("id", "source_ai_draft_id")
    }
    states = {}
    for draft_id in draft_ids:
        improvement_id = imported.get(draft_id)
        if improvement_id:
            states[draft_id] = {
                "imported": True,
                "url": f"{reverse('feedback:improvement-list')}?survey={survey.slug}#improvement-{improvement_id}",
            }
        else:
            states[draft_id] = {
                "imported": False,
                "url": reverse(
                    "feedback:ai-stage-improvement-draft",
                    args=[survey.slug, stage_id, draft_id],
                ),
            }
    return states


def get_published_analysis_payload(survey):
    """Read pointers and bounded display JSON without scanning source rows."""

    state = (
        SurveyAnalysisState.objects.select_related(
            "published_snapshot",
            "published_ai_stage__snapshot",
            "survey__analysis_source__active_external_version",
        )
        .filter(survey=survey)
        .first()
    )
    latest_job = (
        AnalysisJob.objects.filter(survey=survey)
        .order_by("-created_at", "-id")
        .values("id", "status", "executor", "error_code", "created_at", "updated_at")
        .first()
    )
    if state is None:
        return {
            "available": False,
            "versions": {"input": 0, "config": 0, "pipeline": ""},
            "freshness": {"statistics": False, "text": False, "ai": False},
            "statistics": {},
            "text_analysis": {},
            "ai": None,
            "latest_job": latest_job,
        }
    try:
        binding = resolve_analysis_source(state.survey)
    except AnalysisSourceConfigurationError:
        binding = None
    display = state.published_display_payload if isinstance(state.published_display_payload, dict) else {}
    statistics = display.get("statistics") if isinstance(display.get("statistics"), dict) else {}
    text_analysis = display.get("text_analysis") if isinstance(display.get("text_analysis"), dict) else {}
    ai_snapshot = state.published_ai_stage.snapshot if state.published_ai_stage_id else None
    return {
        "available": bool(state.published_snapshot_id and display),
        "snapshot_id": state.published_snapshot_id,
        "published_at": state.published_at.isoformat() if state.published_at else None,
        "versions": {
            "input": state.input_version,
            "config": state.config_version,
            "pipeline": state.pipeline_version,
            "published": state.publication_manifest,
        },
        "freshness": {
            "statistics": _stage_is_current(state, "statistics", binding=binding or False),
            "text": _stage_is_current(state, "text", binding=binding or False),
            "ai": _stage_is_current(state, "ai", binding=binding or False),
        },
        "statistics": statistics,
        "text_analysis": text_analysis,
        "snapshot": display.get("snapshot") if isinstance(display.get("snapshot"), dict) else {},
        "ai": state.published_ai_payload or None,
        "ai_source": (
            {
                "snapshot_id": ai_snapshot.pk,
                "source_latest_at": ai_snapshot.source_latest_at.isoformat()
                if ai_snapshot.source_latest_at
                else None,
                "response_count": ai_snapshot.response_count,
                "analysis_coverage": float(ai_snapshot.analysis_coverage),
                "model_name": state.published_ai_stage.model_name,
            }
            if ai_snapshot
            else {}
        ),
        "latest_job": latest_job,
    }


def get_published_ai_pipeline_status(survey):
    """Adapt finite publication state to the existing dashboard JSON contract."""

    publication = get_published_analysis_payload(survey)
    meta = publication.get("snapshot") or {}
    ai_meta = publication.get("ai_source") or {}
    ai = publication.get("ai")
    freshness = publication["freshness"]
    response_count = int(meta.get("response_count") or 0)
    latest_job = publication.get("latest_job") or {}
    ai_job_status = latest_job.get("status") if latest_job.get("executor") == AnalysisJob.Executor.AI else None
    base_is_current = bool(freshness["statistics"] and freshness["text"])
    current_ai = bool(ai and freshness["ai"])
    has_enough_data = response_count >= settings.AI_REPORT_MIN_RESPONSES
    stages = {
        "statistics": {
            "status": "succeeded" if publication["statistics"] else "not_started",
            "stage_id": None,
            "revision": None,
            "cache_hit": False,
            "is_current": freshness["statistics"],
        },
        "text": {
            "status": "succeeded" if publication["text_analysis"] else "not_started",
            "stage_id": None,
            "revision": None,
            "cache_hit": False,
            "is_current": freshness["text"],
        },
        "synthesis": {
            "status": "succeeded" if ai else "not_started",
            "stage_id": ai.get("stage_id") if ai else None,
            "revision": None,
            "cache_hit": False,
            "is_current": current_ai,
        },
    }
    report = None
    if ai:
        draft_states = _published_draft_states(survey, ai, current_ai=current_ai)
        report = {
            "snapshot_id": ai_meta.get("snapshot_id"),
            "survey_slug": survey.slug,
            "survey_title": survey.title,
            "status": "succeeded",
            "model": ai.get("model_name") or ai_meta.get("model_name"),
            "generated_at": ai.get("generated_at"),
            "source_latest_at": ai_meta.get("source_latest_at"),
            "response_count": int(ai_meta.get("response_count") or 0),
            "analysis_coverage": ai_meta.get("analysis_coverage") or 0,
            "is_current": current_ai,
            "cache_hit": False,
            "generation_ms": None,
            "generation_profile": "background",
            "report_source": "staged",
            "evidence_coverage": {},
            "content": ai,
            "draft_states": draft_states,
            "draft_urls": {},
        }
    return {
        "survey": {
            "slug": survey.slug,
            "title": survey.title,
            "response_count": response_count,
            "valid_response_count": response_count,
        },
        "freshness": {
            "is_current": current_ai,
            "base_is_current": base_is_current,
            "has_new_data": has_enough_data and not current_ai,
            "has_enough_data": has_enough_data,
            "latest_analysis_incomplete": has_enough_data and not current_ai,
            "latest_ai_status": "succeeded" if current_ai else ai_job_status or "not_started",
            "latest_error_code": latest_job.get("error_code", "") if ai_job_status == "failed" else "",
            "latest_error_message": "背景 AI 工作未完成；上一版成功結果不受影響。" if ai_job_status == "failed" else "",
            "minimum_responses": settings.AI_REPORT_MIN_RESPONSES,
            "fingerprint_ms": None,
        },
        "stages": stages,
        "report": report,
        "background_mode": True,
        "auto_ai_enabled": settings.ANALYSIS_AUTO_AI_ENABLED,
        "workflow": {
            "mode": "local_worker",
            "website_role": "published_read_only",
            "latest_job": latest_job,
        },
        "publication": {
            "snapshot_id": publication.get("snapshot_id"),
            "published_at": publication.get("published_at"),
            "versions": publication.get("versions"),
        },
    }
