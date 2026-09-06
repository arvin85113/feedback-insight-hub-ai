"""Single-job deterministic worker built on the shared AnalysisInput contract."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import time

from django.conf import settings
from django.db import transaction
from django.db.models import Max

from .analysis_adapters import AnswerInput, ParquetInput
from .analysis_jobs import heartbeat_job, publish_analysis_snapshot
from .ai_snapshot_service import SNAPSHOT_SCHEMA_VERSION, current_prompt_version
from .background_analysis import (
    PROFILE_PATH,
    assemble_input,
    calculate_statistics,
    calculate_text,
    descriptors,
    digest,
    pipeline_version,
)
from .models import AnalysisJob, SurveyAIAnalysisStage, SurveyAIReportSnapshot


DISPLAY_SCHEMA_VERSION = "analysis-display-v1"
MAX_DISPLAY_CHARTS = 100
MAX_DISPLAY_TESTS = 100
MAX_DISPLAY_COUNTS = 50
MAX_DISPLAY_CATEGORIES = 50


class WorkerExecutionError(RuntimeError):
    def __init__(self, code, *, retryable=False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class WorkerCancelled(WorkerExecutionError):
    def __init__(self):
        super().__init__("cancelled", retryable=False)


@dataclass(frozen=True)
class ExternalInputSpec:
    manifest_path: Path
    mapping_path: Path


@dataclass(frozen=True)
class WorkerRunResult:
    job_id: int
    artifact_path: Path
    artifact_sha256: str
    cache_hit: bool
    input_rows: int
    snapshot_id: int | None
    published: bool
    stale: bool
    timings_seconds: dict


def _assert_lease(job, lease_seconds):
    heartbeat = heartbeat_job(job.pk, job.lease_token, lease_seconds=lease_seconds)
    if heartbeat.cancel_requested:
        raise WorkerCancelled
    if not heartbeat.accepted:
        raise WorkerExecutionError("lease_lost", retryable=True)


def _build_adapter(job, external_inputs):
    if job.source_kind == AnalysisJob.SourceKind.ANSWERS:
        version = (
            f"survey:{job.survey_id}:input:{job.input_version}:"
            f"config:{job.config_version}:pipeline:{job.pipeline_version}"
        )
        return AnswerInput.from_survey(job.survey, version=version)
    spec = (external_inputs or {}).get(job.source_ref)
    if spec is None:
        raise WorkerExecutionError("external_source_not_configured")
    adapter = ParquetInput(spec.manifest_path, spec.mapping_path)
    if adapter.dataset_version != job.source_version:
        raise WorkerExecutionError("external_source_version_mismatch")
    return adapter


def _serialise_statistics(payload):
    charts = []
    for chart in payload.get("charts", [])[:MAX_DISPLAY_CHARTS]:
        question = chart.get("question")
        counts = list(chart.get("counts") or [])
        charts.append(
            {
                **chart,
                "question": {
                    "title": getattr(question, "title", ""),
                    "kind": getattr(question, "kind", ""),
                    "data_type": getattr(question, "data_type", ""),
                },
                "counts": counts[:MAX_DISPLAY_COUNTS],
                "counts_truncated": len(counts) > MAX_DISPLAY_COUNTS,
            }
        )
    tests = list(payload.get("inferential_analysis") or [])
    return {
        "charts": charts,
        "charts_truncated": len(payload.get("charts") or []) > MAX_DISPLAY_CHARTS,
        "inferential_analysis": tests[:MAX_DISPLAY_TESTS],
        "inferential_analysis_truncated": len(tests) > MAX_DISPLAY_TESTS,
        "question_analysis": list(payload.get("question_analysis") or [])[:MAX_DISPLAY_CHARTS],
        "available_tests_count": payload.get("available_tests_count", 0),
        "skipped_tests_count": payload.get("skipped_tests_count", 0),
        "field_coverage": payload.get("field_coverage", {}),
    }


def _serialise_text(payload):
    return {
        "keywords": list(payload.get("keywords") or [])[:20],
        "summary": payload.get("summary") or {},
        "category_sentiments": list(payload.get("category_sentiments") or [])[:MAX_DISPLAY_CATEGORIES],
        "coverage": payload.get("coverage") or {},
        "profile_version": payload.get("profile_version"),
        "limitations": payload.get("limitations"),
    }


def _target_identity(job, adapter, implementation_version):
    return {
        "survey_id": job.survey_id,
        "source_kind": job.source_kind,
        "source_ref": job.source_ref,
        "source_version": job.source_version or adapter.dataset_version,
        "adapter_version": adapter.dataset_version,
        "input_version": job.input_version,
        "config_version": job.config_version,
        "pipeline_contract_version": job.pipeline_version,
        "pipeline_implementation_version": implementation_version,
        "fields": [vars(field) for field in adapter.fields()],
    }


def _read_cached_artifact(path, target):
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerExecutionError("artifact_cache_unreadable") from exc
    result = envelope.get("result") or {}
    if envelope.get("sha256") != digest(result) or digest(result.get("target")) != digest(target):
        raise WorkerExecutionError("artifact_cache_integrity_mismatch")
    return envelope


def _write_artifact(output_root, target, build_result):
    version = digest(target)
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{version}.json"
    if destination.exists():
        return destination, _read_cached_artifact(destination, target), True
    lock = root / f"{version}.lock"
    partial = root / f"{version}.part"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise WorkerExecutionError("artifact_busy", retryable=True) from exc
    os.close(descriptor)
    try:
        result = build_result(version)
        envelope = {"sha256": digest(result), "result": result}
        with partial.open("x", encoding="utf-8") as handle:
            json.dump(envelope, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, destination)
        return destination, envelope, False
    finally:
        lock.unlink(missing_ok=True)


def _persist_snapshot(job, result):
    source_snapshot = result["source_snapshot"]
    fingerprint = result["input_fingerprint"]
    latest_at = None
    if job.source_kind == AnalysisJob.SourceKind.ANSWERS:
        latest_at = job.survey.submissions.filter(
            is_complete=True,
            voided_at__isnull=True,
        ).aggregate(value=Max("submitted_at"))["value"]
    lookup = {
        "survey": job.survey,
        "data_fingerprint": fingerprint,
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "prompt_version": current_prompt_version(),
        "model_name": settings.GEMINI_MODEL,
    }
    defaults = {
        "source_snapshot": source_snapshot,
        "status": SurveyAIReportSnapshot.Status.SNAPSHOT_READY,
        "response_count": result["input_rows"],
        "analysis_coverage": source_snapshot["data_scope"].get("analysis_coverage") or 0,
        "source_latest_at": latest_at,
        "snapshot_ms": round(result["timings_seconds"]["total"] * 1000),
        "attempt_count": job.attempt_count,
    }
    with transaction.atomic():
        snapshot, created = SurveyAIReportSnapshot.objects.get_or_create(**lookup, defaults=defaults)
        if created:
            return snapshot
        if snapshot.source_snapshot:
            if digest(snapshot.source_snapshot) != digest(source_snapshot):
                raise WorkerExecutionError("snapshot_fingerprint_collision")
            if snapshot.status == SurveyAIReportSnapshot.Status.BUILDING:
                raise WorkerExecutionError("snapshot_busy", retryable=True)
            return snapshot
        raise WorkerExecutionError("snapshot_incomplete", retryable=True)


def execute_deterministic_job(
    job,
    *,
    output_root,
    external_inputs=None,
    lease_seconds=600,
):
    """Run one already-claimed deterministic job and publish its Snapshot pointer."""

    if job.status != AnalysisJob.Status.RUNNING or not job.lease_token:
        raise WorkerExecutionError("job_not_claimed")
    if job.executor != AnalysisJob.Executor.DETERMINISTIC:
        raise WorkerExecutionError("unsupported_executor")
    if not set(job.requested_stages) <= {
        SurveyAIAnalysisStage.StageType.STATISTICS,
        SurveyAIAnalysisStage.StageType.TEXT,
    }:
        raise WorkerExecutionError("unsupported_stage")
    _assert_lease(job, lease_seconds)
    adapter = _build_adapter(job, external_inputs)
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    try:
        implementation_version = digest(
            {
                "analysis": pipeline_version(profile),
                "worker": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            }
        )
    except FileNotFoundError as exc:
        raise WorkerExecutionError("pipeline_resource_missing") from exc
    target = _target_identity(job, adapter, implementation_version)

    def build_result(input_fingerprint):
        started = time.perf_counter()
        questions = descriptors(adapter)
        statistics, input_rows = calculate_statistics(adapter, questions)
        after_statistics = time.perf_counter()
        _assert_lease(job, lease_seconds)
        text = calculate_text(adapter, profile)
        after_text = time.perf_counter()
        _assert_lease(job, lease_seconds)
        source_snapshot = assemble_input(
            adapter,
            questions,
            statistics,
            text,
            input_rows,
            input_fingerprint,
        )
        source_snapshot["data_scope"].update(
            {
                "survey_slug": job.survey.slug,
                "survey_title": job.survey.title,
                "input_version": job.input_version,
                "config_version": job.config_version,
                "pipeline_contract_version": job.pipeline_version,
                "pipeline_implementation_version": implementation_version,
            }
        )
        source_snapshot["display_payload"] = {
            "schema_version": DISPLAY_SCHEMA_VERSION,
            "statistics": _serialise_statistics(statistics),
            "text_analysis": _serialise_text(text),
        }
        if hasattr(adapter, "verify_unchanged"):
            adapter.verify_unchanged()
        finished = time.perf_counter()
        return {
            "target": target,
            "input_fingerprint": input_fingerprint,
            "input_rows": input_rows,
            "source_snapshot": source_snapshot,
            "timings_seconds": {
                "statistics": after_statistics - started,
                "text": after_text - after_statistics,
                "assembly": finished - after_text,
                "total": finished - started,
            },
        }

    artifact_path, envelope, cache_hit = _write_artifact(output_root, target, build_result)
    result = envelope["result"]
    if hasattr(adapter, "verify_unchanged"):
        adapter.verify_unchanged()
    _assert_lease(job, lease_seconds)
    snapshot = _persist_snapshot(job, result)
    _assert_lease(job, lease_seconds)
    publication = publish_analysis_snapshot(
        job.pk,
        job.lease_token,
        snapshot_id=snapshot.pk,
        input_fingerprint=result["input_fingerprint"],
        artifact_manifest={
            "sha256": envelope["sha256"],
            "size": artifact_path.stat().st_size,
            "input_rows": result["input_rows"],
            "cache_hit": cache_hit,
            "pipeline_implementation_version": implementation_version,
        },
        queue_ai=settings.ANALYSIS_AUTO_AI_ENABLED,
    )
    return WorkerRunResult(
        job_id=job.pk,
        artifact_path=artifact_path,
        artifact_sha256=envelope["sha256"],
        cache_hit=cache_hit,
        input_rows=result["input_rows"],
        snapshot_id=snapshot.pk if publication.published else None,
        published=publication.published,
        stale=publication.stale,
        timings_seconds=result["timings_seconds"],
    )
