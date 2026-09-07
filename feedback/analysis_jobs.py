"""Versioned background-analysis queue and publication coordination.

This module only coordinates durable state.  It does not run analytics, call an
AI provider, or expose a worker service port.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
import json
import re
import uuid

from django.db import connection, transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import AnalysisJob, Survey, SurveyAIAnalysisStage, SurveyAIReportSnapshot, SurveyAnalysisState


PIPELINE_CONTRACT_VERSION = "analysis-publish-v1"
DEFAULT_STAGES = (
    SurveyAIAnalysisStage.StageType.STATISTICS,
    SurveyAIAnalysisStage.StageType.TEXT,
)
ALL_STAGES = frozenset(SurveyAIAnalysisStage.StageType.values)
BASE_STAGES = frozenset(DEFAULT_STAGES)
_scheduling_suppressed = ContextVar("analysis_scheduling_suppressed", default=False)


class AnalysisJobError(RuntimeError):
    pass


@dataclass(frozen=True)
class HeartbeatResult:
    accepted: bool
    cancel_requested: bool = False


@dataclass(frozen=True)
class PublishResult:
    published: bool
    stale: bool = False


@contextmanager
def suppress_analysis_scheduling():
    """Coalesce signal-driven invalidations inside a known bulk operation."""

    token = _scheduling_suppressed.set(True)
    try:
        yield
    finally:
        _scheduling_suppressed.reset(token)


def scheduling_is_suppressed():
    return _scheduling_suppressed.get()


def _normalise_stages(stages):
    stages = tuple(dict.fromkeys(stages or DEFAULT_STAGES))
    unknown = set(stages) - ALL_STAGES
    if unknown:
        raise ValueError(f"不支援的分析階段：{', '.join(sorted(unknown))}")
    return list(stages)


def _merge_stages(current, requested):
    wanted = set(current or ()) | set(requested)
    return [stage for stage in SurveyAIAnalysisStage.StageType.values if stage in wanted]


def _executor_for(stages):
    wanted = set(stages)
    if wanted and wanted <= BASE_STAGES:
        return AnalysisJob.Executor.DETERMINISTIC
    if wanted == {SurveyAIAnalysisStage.StageType.SYNTHESIS}:
        return AnalysisJob.Executor.AI
    raise ValueError("統計／文字與 AI synthesis 必須使用獨立工作")


def _ensure_pending_job_locked(
    survey,
    state,
    *,
    source_kind,
    source_ref,
    source_version,
    requested_stages,
    executor=None,
):
    executor = executor or _executor_for(requested_stages)
    pending = (
        AnalysisJob.objects.select_for_update()
        .filter(
            survey=survey,
            source_kind=source_kind,
            source_ref=source_ref,
            executor=executor,
            status=AnalysisJob.Status.PENDING,
        )
        .first()
    )
    if pending:
        pending.input_version = state.input_version
        pending.config_version = state.config_version
        pending.pipeline_version = state.pipeline_version
        if source_version:
            pending.source_version = source_version
        pending.requested_stages = _merge_stages(pending.requested_stages, requested_stages)
        pending.available_at = timezone.now()
        pending.error_code = ""
        pending.save(
            update_fields=(
                "input_version",
                "config_version",
                "pipeline_version",
                "source_version",
                "requested_stages",
                "available_at",
                "error_code",
                "updated_at",
            )
        )
        return pending
    return AnalysisJob.objects.create(
        survey=survey,
        source_kind=source_kind,
        source_ref=source_ref,
        source_version=source_version,
        executor=executor,
        input_version=state.input_version,
        config_version=state.config_version,
        pipeline_version=state.pipeline_version,
        requested_stages=requested_stages,
    )


def schedule_survey_analysis(
    survey_id,
    *,
    change="input",
    source_kind=AnalysisJob.SourceKind.ANSWERS,
    source_ref="",
    source_version="",
    requested_stages=DEFAULT_STAGES,
    pipeline_version=PIPELINE_CONTRACT_VERSION,
):
    """Bump stored versions and create or coalesce one pending job.

    Call inside the mutation transaction.  The job becomes visible to workers
    only if that transaction commits.
    """

    if scheduling_is_suppressed():
        return None
    if change not in {"none", "input", "config", "both"}:
        raise ValueError("change 必須是 none、input、config 或 both")
    if source_kind not in AnalysisJob.SourceKind.values:
        raise ValueError("不支援的資料來源種類")
    source_ref = str(source_ref or "")
    source_version = str(source_version or "")
    if source_kind == AnalysisJob.SourceKind.EXTERNAL and not (source_ref and source_version):
        raise ValueError("外部資料工作必須提供 source_ref 與 source_version")
    stages = _normalise_stages(requested_stages)
    executor = _executor_for(stages)

    with transaction.atomic():
        survey = Survey.objects.select_for_update().filter(pk=survey_id).first()
        if not survey:
            return None
        state, _ = SurveyAnalysisState.objects.get_or_create(survey=survey)
        if change in {"input", "both"}:
            state.input_version += 1
        if change in {"config", "both"}:
            state.config_version += 1
        if state.pipeline_version and state.pipeline_version != pipeline_version:
            state.config_version += 1
        state.pipeline_version = pipeline_version
        state.save(update_fields=("input_version", "config_version", "pipeline_version", "updated_at"))
        if not survey.analysis_enabled or survey.archived_at is not None:
            AnalysisJob.objects.filter(
                survey=survey,
                status=AnalysisJob.Status.PENDING,
            ).update(
                status=AnalysisJob.Status.CANCELLED,
                error_code="analysis_disabled",
                finished_at=timezone.now(),
            )
            return None
        return _ensure_pending_job_locked(
            survey,
            state,
            source_kind=source_kind,
            source_ref=source_ref,
            source_version=source_version,
            requested_stages=stages,
            executor=executor,
        )


def claim_next_job(
    worker_id,
    *,
    lease_seconds=120,
    executor=None,
    external_source_refs=None,
    survey_ids=None,
    source_kind=None,
    source_ref=None,
):
    if not worker_id or len(worker_id) > 128:
        raise ValueError("worker_id 必須是 1 到 128 個字元")
    if lease_seconds < 5:
        raise ValueError("lease_seconds 不得小於 5")
    if executor is not None and executor not in AnalysisJob.Executor.values:
        raise ValueError("不支援的 executor")
    if source_kind is not None and source_kind not in AnalysisJob.SourceKind.values:
        raise ValueError("不支援的資料來源種類")
    now = timezone.now()
    with transaction.atomic():
        AnalysisJob.objects.filter(
            status=AnalysisJob.Status.RUNNING,
            lease_expires_at__lt=now,
            attempt_count__gte=F("max_attempts"),
        ).update(
            status=AnalysisJob.Status.FAILED,
            error_code="attempts_exhausted",
            finished_at=now,
            lease_token=None,
            worker_id="",
            updated_at=now,
        )
        eligible = AnalysisJob.objects.filter(
            Q(status=AnalysisJob.Status.PENDING, available_at__lte=now)
            | Q(
                status=AnalysisJob.Status.RUNNING,
                lease_expires_at__lt=now,
                attempt_count__lt=F("max_attempts"),
            ),
            cancel_requested_at__isnull=True,
        ).order_by("available_at", "created_at", "id")
        if executor is not None:
            eligible = eligible.filter(executor=executor)
        if survey_ids is not None:
            survey_ids = tuple(survey_ids)
            if not survey_ids:
                return None
            eligible = eligible.filter(survey_id__in=survey_ids)
        if source_kind is not None:
            eligible = eligible.filter(source_kind=source_kind)
        if source_ref is not None:
            eligible = eligible.filter(source_ref=str(source_ref))
        if external_source_refs is not None:
            eligible = eligible.filter(
                Q(source_kind=AnalysisJob.SourceKind.ANSWERS)
                | Q(
                    source_kind=AnalysisJob.SourceKind.EXTERNAL,
                    source_ref__in=tuple(external_source_refs),
                )
            )
        if connection.features.has_select_for_update_skip_locked:
            eligible = eligible.select_for_update(skip_locked=True)
        else:
            eligible = eligible.select_for_update()
        job = eligible.first()
        if not job:
            return None
        job.status = AnalysisJob.Status.RUNNING
        job.worker_id = worker_id
        job.lease_token = uuid.uuid4()
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        job.heartbeat_at = now
        job.attempt_count += 1
        job.started_at = job.started_at or now
        job.error_code = ""
        job.save(
            update_fields=(
                "status",
                "worker_id",
                "lease_token",
                "lease_expires_at",
                "heartbeat_at",
                "attempt_count",
                "started_at",
                "error_code",
                "updated_at",
            )
        )
        return job


def heartbeat_job(job_id, lease_token, *, lease_seconds=120):
    if lease_seconds < 5:
        raise ValueError("lease_seconds 不得小於 5")
    now = timezone.now()
    with transaction.atomic():
        job = AnalysisJob.objects.select_for_update().filter(pk=job_id).first()
        if not job or job.status != AnalysisJob.Status.RUNNING or job.lease_token != lease_token:
            return HeartbeatResult(False)
        if job.cancel_requested_at:
            return HeartbeatResult(False, cancel_requested=True)
        if not job.lease_expires_at or job.lease_expires_at < now:
            return HeartbeatResult(False)
        job.heartbeat_at = now
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        job.save(update_fields=("heartbeat_at", "lease_expires_at", "updated_at"))
        return HeartbeatResult(True)


def request_job_cancel(job_id):
    now = timezone.now()
    with transaction.atomic():
        job = AnalysisJob.objects.select_for_update().get(pk=job_id)
        if job.status == AnalysisJob.Status.PENDING:
            job.status = AnalysisJob.Status.CANCELLED
            job.cancel_requested_at = now
            job.finished_at = now
            job.error_code = "cancelled_by_request"
            job.save(
                update_fields=(
                    "status",
                    "cancel_requested_at",
                    "finished_at",
                    "error_code",
                    "updated_at",
                )
            )
        elif job.status == AnalysisJob.Status.RUNNING and not job.cancel_requested_at:
            job.cancel_requested_at = now
            job.save(update_fields=("cancel_requested_at", "updated_at"))
        return job


def finish_cancelled_job(job_id, lease_token):
    now = timezone.now()
    updated = AnalysisJob.objects.filter(
        pk=job_id,
        status=AnalysisJob.Status.RUNNING,
        lease_token=lease_token,
        cancel_requested_at__isnull=False,
    ).update(
        status=AnalysisJob.Status.CANCELLED,
        error_code="cancelled_by_request",
        finished_at=now,
        lease_token=None,
        lease_expires_at=None,
        worker_id="",
        updated_at=now,
    )
    return bool(updated)


def _safe_error_code(error_code):
    value = re.sub(r"[^a-z0-9_.-]+", "_", str(error_code or "worker_failed").lower()).strip("_")
    return (value or "worker_failed")[:64]


def fail_job(job_id, lease_token, *, error_code, retryable=False, retry_delay_seconds=0):
    now = timezone.now()
    with transaction.atomic():
        job = AnalysisJob.objects.select_for_update().filter(pk=job_id).first()
        if not job or job.status != AnalysisJob.Status.RUNNING or job.lease_token != lease_token:
            return False
        if job.cancel_requested_at:
            status = AnalysisJob.Status.CANCELLED
            retryable = False
        elif retryable and job.attempt_count < job.max_attempts:
            status = AnalysisJob.Status.PENDING
        else:
            status = AnalysisJob.Status.FAILED
        job.status = status
        job.error_code = "cancelled_by_request" if status == AnalysisJob.Status.CANCELLED else _safe_error_code(error_code)
        job.available_at = now + timedelta(seconds=max(0, retry_delay_seconds))
        job.finished_at = now if status in {AnalysisJob.Status.FAILED, AnalysisJob.Status.CANCELLED} else None
        job.worker_id = ""
        job.lease_token = None
        job.lease_expires_at = None
        job.save(
            update_fields=(
                "status",
                "error_code",
                "available_at",
                "finished_at",
                "worker_id",
                "lease_token",
                "lease_expires_at",
                "updated_at",
            )
        )
        return True


def _is_mock_stage(stage):
    model = (stage.model_name or "").strip().lower()
    output = stage.output_json if isinstance(stage.output_json, dict) else {}
    return model == "mock" or model.startswith("mock-") or output.get("ai_mode") == "mock"


def _versions_are_current(job, state):
    return (
        state.input_version == job.input_version
        and state.config_version == job.config_version
        and state.pipeline_version == job.pipeline_version
    )


def _published_base_is_current(state, job):
    manifest = state.publication_manifest or {}
    for stage_type in BASE_STAGES:
        item = manifest.get(stage_type) or {}
        if (
            item.get("snapshot_id") != state.published_snapshot_id
            or item.get("input_version") != job.input_version
            or item.get("config_version") != job.config_version
            or item.get("pipeline_version") != job.pipeline_version
            or item.get("source_kind", AnalysisJob.SourceKind.ANSWERS) != job.source_kind
            or item.get("source_ref", "") != job.source_ref
            or item.get("source_version", "") != job.source_version
        ):
            return False
    return bool(state.published_snapshot_id)


def _supersede_job_locked(job, state, now):
    _ensure_pending_job_locked(
        job.survey,
        state,
        source_kind=job.source_kind,
        source_ref=job.source_ref,
        source_version=job.source_version,
        requested_stages=_normalise_stages(job.requested_stages),
        executor=job.executor,
    )
    job.status = AnalysisJob.Status.CANCELLED
    job.error_code = "superseded"
    job.finished_at = now
    job.worker_id = ""
    job.lease_token = None
    job.lease_expires_at = None
    job.save(
        update_fields=(
            "status",
            "error_code",
            "finished_at",
            "worker_id",
            "lease_token",
            "lease_expires_at",
            "updated_at",
        )
    )


def _finish_published_job_locked(
    job,
    snapshot,
    now,
    *,
    input_fingerprint,
    stage_ids=None,
    artifact_manifest=None,
):
    job.status = AnalysisJob.Status.SUCCEEDED
    job.input_fingerprint = str(input_fingerprint or snapshot.data_fingerprint)[:64]
    job.result_manifest = {
        "snapshot_id": snapshot.pk,
        "stage_ids": dict(stage_ids or {}),
        "published_at": now.isoformat(),
    }
    if artifact_manifest:
        job.result_manifest["artifact"] = dict(artifact_manifest)
    job.finished_at = now
    job.worker_id = ""
    job.lease_token = None
    job.lease_expires_at = None
    job.save(
        update_fields=(
            "status",
            "input_fingerprint",
            "result_manifest",
            "finished_at",
            "worker_id",
            "lease_token",
            "lease_expires_at",
            "updated_at",
        )
    )


def _lock_current_claim(job_id, lease_token, now):
    job = AnalysisJob.objects.select_for_update().select_related("survey").get(pk=job_id)
    if job.status != AnalysisJob.Status.RUNNING or job.lease_token != lease_token:
        raise AnalysisJobError("工作租約無效")
    if job.cancel_requested_at or not job.lease_expires_at or job.lease_expires_at < now:
        raise AnalysisJobError("工作已取消或租約已過期")
    state = SurveyAnalysisState.objects.select_for_update().get(survey=job.survey)
    if not _versions_are_current(job, state):
        _supersede_job_locked(job, state, now)
        return job, state, False
    return job, state, True


def check_claim_current(job_id, lease_token):
    """Recheck ownership and versions; stale claims are cancelled and re-queued."""

    with transaction.atomic():
        _, _, current = _lock_current_claim(job_id, lease_token, timezone.now())
        return current


def _bounded_json(value, *, depth=0):
    """Copy display-only JSON while enforcing conservative structural limits."""

    if depth >= 8:
        return None
    if isinstance(value, str):
        return value[:4000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        return [_bounded_json(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, dict):
        return {
            str(key)[:128]: _bounded_json(item, depth=depth + 1)
            for key, item in list(value.items())[:100]
        }
    return str(value)[:4000]


def _published_display_payload(snapshot):
    source = snapshot.source_snapshot if isinstance(snapshot.source_snapshot, dict) else {}
    display = source.get("display_payload")
    if not isinstance(display, dict):
        return {}
    bounded = _bounded_json(
        {
            "schema_version": display.get("schema_version"),
            "snapshot": {
                "status": snapshot.status,
                "response_count": snapshot.response_count,
                "analysis_coverage": float(snapshot.analysis_coverage),
                "source_latest_at": snapshot.source_latest_at.isoformat() if snapshot.source_latest_at else None,
                "generated_at": snapshot.generated_at.isoformat() if snapshot.generated_at else None,
                "model_name": snapshot.model_name,
            },
            "statistics": display.get("statistics") or {},
            "text_analysis": display.get("text_analysis") or {},
        }
    )
    if len(json.dumps(bounded, ensure_ascii=False).encode("utf-8")) > 2_000_000:
        raise AnalysisJobError("分析展示資料超過發布上限")
    return bounded


def _published_ai_payload(stage):
    output = stage.output_json if isinstance(stage.output_json, dict) else {}
    evidence_fields = (
        "id",
        "kind",
        "label",
        "value",
        "unit",
        "sample_size",
        "metric_type",
        "test_ref",
        "method_key",
        "test_name",
        "variables",
        "category",
        "occurrence_count",
        "response_count",
        "display_text",
    )

    def safe_evidence(rows):
        return [
            {key: row.get(key) for key in evidence_fields if key in row}
            for row in list(rows or [])[:4]
            if isinstance(row, dict)
        ]

    findings = []
    for row in list(output.get("combined_findings") or [])[:20]:
        if not isinstance(row, dict):
            continue
        findings.append(
            {
                key: row.get(key)
                for key in (
                    "title",
                    "source_stages",
                    "priority",
                    "rationale",
                    "evidence_refs",
                    "data_limitations",
                )
                if key in row
            }
        )
        findings[-1]["evidence"] = safe_evidence(row.get("evidence"))

    drafts = []
    for row in list(output.get("improvement_drafts") or [])[:10]:
        if not isinstance(row, dict):
            continue
        drafts.append(
            {
                key: row.get(key)
                for key in (
                    "draft_id",
                    "title",
                    "summary",
                    "related_category",
                    "priority",
                    "rationale",
                    "acceptance_criteria",
                    "evidence_refs",
                    "data_limitations",
                )
                if key in row
            }
        )
        drafts[-1]["evidence"] = safe_evidence(row.get("evidence"))

    bounded = _bounded_json(
        {
            "executive_summary": output.get("executive_summary", ""),
            "combined_findings": findings,
            "improvement_drafts": drafts,
            "data_caveats": list(output.get("data_caveats") or [])[:20],
            "stage_id": stage.pk,
            "generated_at": stage.generated_at.isoformat() if stage.generated_at else None,
            "model_name": stage.model_name,
        }
    )
    if len(json.dumps(bounded, ensure_ascii=False).encode("utf-8")) > 1_000_000:
        raise AnalysisJobError("AI 展示資料超過發布上限")
    return bounded


def publish_analysis_snapshot(
    job_id,
    lease_token,
    *,
    snapshot_id,
    input_fingerprint="",
    artifact_manifest=None,
    queue_ai=False,
):
    """Publish deterministic statistics/text held in an existing Snapshot."""

    now = timezone.now()
    with transaction.atomic():
        job, state, current = _lock_current_claim(job_id, lease_token, now)
        if not current:
            return PublishResult(False, stale=True)
        requested = set(job.requested_stages)
        base_stages = {
            SurveyAIAnalysisStage.StageType.STATISTICS,
            SurveyAIAnalysisStage.StageType.TEXT,
        }
        if not requested or not requested <= base_stages:
            raise AnalysisJobError("此工作不是統計／文字快照發布工作")
        snapshot = SurveyAIReportSnapshot.objects.get(pk=snapshot_id, survey=job.survey)
        if snapshot.status not in {
            SurveyAIReportSnapshot.Status.SNAPSHOT_READY,
            SurveyAIReportSnapshot.Status.GENERATING,
            SurveyAIReportSnapshot.Status.SUCCEEDED,
            SurveyAIReportSnapshot.Status.FAILED,
        }:
            raise AnalysisJobError("分析快照尚未完成")
        if input_fingerprint and input_fingerprint != snapshot.data_fingerprint:
            raise AnalysisJobError("輸入指紋與快照不符")
        required_sections = {
            SurveyAIAnalysisStage.StageType.STATISTICS: "statistics",
            SurveyAIAnalysisStage.StageType.TEXT: "text_analysis",
        }
        if any(required_sections[stage] not in snapshot.source_snapshot for stage in requested):
            raise AnalysisJobError("分析快照缺少要求的結果區段")

        manifest = dict(state.publication_manifest or {})
        for stage_type in requested:
            manifest[stage_type] = {
                "kind": "snapshot",
                "snapshot_id": snapshot.pk,
                "input_version": job.input_version,
                "config_version": job.config_version,
                "pipeline_version": job.pipeline_version,
                "source_kind": job.source_kind,
                "source_ref": job.source_ref,
                "source_version": job.source_version,
                "published_at": now.isoformat(),
            }
        state.published_snapshot = snapshot
        state.published_display_payload = _published_display_payload(snapshot)
        state.publication_manifest = manifest
        state.published_at = now
        state.save(
            update_fields=(
                "published_snapshot",
                "published_display_payload",
                "publication_manifest",
                "published_at",
                "updated_at",
            )
        )
        if queue_ai:
            _ensure_pending_job_locked(
                job.survey,
                state,
                source_kind=job.source_kind,
                source_ref=job.source_ref,
                source_version=job.source_version,
                requested_stages=[SurveyAIAnalysisStage.StageType.SYNTHESIS],
                executor=AnalysisJob.Executor.AI,
            )
        _finish_published_job_locked(
            job,
            snapshot,
            now,
            input_fingerprint=input_fingerprint,
            artifact_manifest=artifact_manifest,
        )
        return PublishResult(True)


def publish_analysis_stages(
    job_id,
    lease_token,
    *,
    snapshot_id,
    stage_ids,
    input_fingerprint="",
):
    """Publish a verified synthesis stage without replacing newer base results."""

    now = timezone.now()
    stage_ids = dict(stage_ids or {})
    synthesis = SurveyAIAnalysisStage.StageType.SYNTHESIS
    if set(stage_ids) != {synthesis}:
        raise AnalysisJobError("AI 發布必須且只能指定 synthesis 階段")
    with transaction.atomic():
        job, state, current = _lock_current_claim(job_id, lease_token, now)
        if not current:
            return PublishResult(False, stale=True)
        if set(job.requested_stages) != {synthesis}:
            raise AnalysisJobError("此工作不是 AI synthesis 發布工作")
        snapshot = SurveyAIReportSnapshot.objects.get(pk=snapshot_id, survey=job.survey)
        if state.published_snapshot_id != snapshot.pk:
            raise AnalysisJobError("AI 階段不屬於目前發布的統計／文字快照")
        if not _published_base_is_current(state, job):
            raise AnalysisJobError("目前發布的統計／文字版本尚未符合 AI 工作")
        stage = SurveyAIAnalysisStage.objects.select_related("snapshot").get(pk=stage_ids[synthesis])
        if stage.stage_type != synthesis or stage.snapshot_id != snapshot.pk:
            raise AnalysisJobError("AI 階段不屬於指定快照")
        if stage.status != SurveyAIAnalysisStage.Status.SUCCEEDED:
            raise AnalysisJobError("只有成功階段可以發布")
        if _is_mock_stage(stage):
            raise AnalysisJobError("mock 結果不得正式發布")
        manifest = dict(state.publication_manifest or {})
        manifest["ai"] = {
            "kind": "stage",
            "snapshot_id": snapshot.pk,
            "stage_id": stage.pk,
            "input_version": job.input_version,
            "config_version": job.config_version,
            "pipeline_version": job.pipeline_version,
            "source_kind": job.source_kind,
            "source_ref": job.source_ref,
            "source_version": job.source_version,
            "model_name": stage.model_name,
            "published_at": now.isoformat(),
        }
        state.published_ai_stage = stage
        state.published_ai_payload = _published_ai_payload(stage)
        state.publication_manifest = manifest
        state.published_at = now
        state.save(
            update_fields=(
                "published_ai_stage",
                "published_ai_payload",
                "publication_manifest",
                "published_at",
                "updated_at",
            )
        )
        _finish_published_job_locked(
            job,
            snapshot,
            now,
            input_fingerprint=input_fingerprint,
            stage_ids={synthesis: stage.pk},
        )
        return PublishResult(True)
