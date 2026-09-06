"""Explicitly authorized single-job Gemini worker."""

from __future__ import annotations

from dataclasses import dataclass

from .ai_stage_service import StageError, generate_stage
from .analysis_jobs import (
    check_claim_current,
    heartbeat_job,
    publish_analysis_stages,
)
from .models import AnalysisJob, SurveyAIAnalysisStage, SurveyAnalysisState


class AIWorkerExecutionError(RuntimeError):
    def __init__(self, code, *, retryable=False, already_finalized=False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.already_finalized = already_finalized


class AIWorkerCancelled(AIWorkerExecutionError):
    def __init__(self):
        super().__init__("cancelled")


class AIWorkerSuperseded(AIWorkerExecutionError):
    def __init__(self):
        super().__init__("superseded", already_finalized=True)


@dataclass(frozen=True)
class AIWorkerRunResult:
    job_id: int
    snapshot_id: int
    stage_ids: dict
    reused_stage_count: int


def _checkpoint(job, lease_seconds):
    heartbeat = heartbeat_job(job.pk, job.lease_token, lease_seconds=lease_seconds)
    if heartbeat.cancel_requested:
        raise AIWorkerCancelled
    if not heartbeat.accepted:
        raise AIWorkerExecutionError("lease_lost", retryable=True)
    if not check_claim_current(job.pk, job.lease_token):
        raise AIWorkerSuperseded


def execute_ai_job(job, *, allow_paid_ai=False, lease_seconds=600, progress=None):
    """Generate the three existing AI stages and publish synthesis.

    The caller must explicitly authorize paid API use.  Provider uncertainty is
    handled inside the stage service and is never promoted to an automatic job
    retry here.
    """

    if not allow_paid_ai:
        raise AIWorkerExecutionError("paid_ai_not_authorized")
    if job.status != AnalysisJob.Status.RUNNING or not job.lease_token:
        raise AIWorkerExecutionError("job_not_claimed")
    if job.executor != AnalysisJob.Executor.AI or set(job.requested_stages) != {
        SurveyAIAnalysisStage.StageType.SYNTHESIS
    }:
        raise AIWorkerExecutionError("unsupported_executor")
    _checkpoint(job, lease_seconds)
    state = (
        SurveyAnalysisState.objects.select_related("published_snapshot")
        .filter(survey=job.survey)
        .first()
    )
    if not state or not state.published_snapshot:
        raise AIWorkerExecutionError("base_snapshot_missing")
    manifest = state.publication_manifest or {}
    for stage_type in (
        SurveyAIAnalysisStage.StageType.STATISTICS,
        SurveyAIAnalysisStage.StageType.TEXT,
    ):
        item = manifest.get(stage_type) or {}
        if (
            item.get("snapshot_id") != state.published_snapshot_id
            or item.get("input_version") != job.input_version
            or item.get("config_version") != job.config_version
            or item.get("pipeline_version") != job.pipeline_version
        ):
            raise AIWorkerExecutionError("base_snapshot_not_current", retryable=True)
    snapshot = state.published_snapshot

    stages = {}
    stage_steps = (
        (SurveyAIAnalysisStage.StageType.STATISTICS, "產生統計解讀", 8, 30),
        (SurveyAIAnalysisStage.StageType.TEXT, "產生文字洞察", 38, 60),
        (SurveyAIAnalysisStage.StageType.SYNTHESIS, "產生綜合解析", 68, 90),
    )
    try:
        for stage_type, label, start_percent, end_percent in stage_steps:
            _checkpoint(job, lease_seconds)
            if progress:
                progress(label, start_percent)
            stages[stage_type] = generate_stage(snapshot, stage_type)
            if progress:
                progress(f"{label}完成", end_percent)
    except StageError as exc:
        # A provider timeout or connection loss has an uncertain billing/result
        # state.  Do not turn it into an automatic whole-job retry.
        raise AIWorkerExecutionError(f"ai_{exc.error_code}", retryable=False) from exc

    _checkpoint(job, lease_seconds)
    if progress:
        progress("驗證並發布 Gemini 結果", 95)
    publication = publish_analysis_stages(
        job.pk,
        job.lease_token,
        snapshot_id=snapshot.pk,
        stage_ids={
            SurveyAIAnalysisStage.StageType.SYNTHESIS: stages[
                SurveyAIAnalysisStage.StageType.SYNTHESIS
            ].pk
        },
        input_fingerprint=snapshot.data_fingerprint,
    )
    if publication.stale:
        raise AIWorkerSuperseded
    if progress:
        progress("Gemini 結果已發布", 100)
    return AIWorkerRunResult(
        job_id=job.pk,
        snapshot_id=snapshot.pk,
        stage_ids={stage_type: stage.pk for stage_type, stage in stages.items()},
        reused_stage_count=sum(bool(stage.reused_from_id) for stage in stages.values()),
    )
