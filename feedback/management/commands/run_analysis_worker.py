"""Outbound-only analysis worker loop for a process supervisor."""

import json
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from feedback.ai_worker import (
    AIWorkerCancelled,
    AIWorkerExecutionError,
    AIWorkerSuperseded,
    execute_ai_job,
)
from feedback.analysis_jobs import claim_next_job, fail_job, finish_cancelled_job
from feedback.analysis_worker import (
    ExternalInputSpec,
    WorkerCancelled,
    WorkerExecutionError,
    execute_deterministic_job,
)
from feedback.models import AnalysisJob


class Command(BaseCommand):
    help = "持續領取背景分析工作；只主動連線，不開放本機服務埠。"

    def add_arguments(self, parser):
        parser.add_argument("--worker-id", required=True, help="不含憑證的穩定 Worker 名稱")
        parser.add_argument("--output", required=True, help="本機版本化分析產物目錄")
        parser.add_argument("--lease-seconds", type=int, default=600)
        parser.add_argument("--poll-seconds", type=float, default=5)
        parser.add_argument("--allow-paid-ai", action="store_true")
        parser.add_argument("--external-source-ref")
        parser.add_argument("--manifest")
        parser.add_argument("--mapping")
        parser.add_argument("--once", action="store_true", help="只輪詢一次，供健康檢查與測試")

    def _event(self, **payload):
        self.stdout.write(json.dumps(payload, ensure_ascii=False))

    def _run_deterministic(self, job, *, output, external_inputs, lease_seconds):
        try:
            result = execute_deterministic_job(
                job,
                output_root=output,
                external_inputs=external_inputs,
                lease_seconds=lease_seconds,
            )
        except WorkerCancelled:
            finish_cancelled_job(job.pk, job.lease_token)
            return {"job_id": job.pk, "executor": job.executor, "status": "cancelled"}
        except WorkerExecutionError as exc:
            fail_job(
                job.pk,
                job.lease_token,
                error_code=exc.code,
                retryable=exc.retryable,
                retry_delay_seconds=30 if exc.retryable else 0,
            )
            return {"job_id": job.pk, "executor": job.executor, "status": "failed", "error_code": exc.code}
        except Exception as exc:
            code = f"worker_{type(exc).__name__.lower()}"[:64]
            fail_job(job.pk, job.lease_token, error_code=code, retryable=False)
            return {"job_id": job.pk, "executor": job.executor, "status": "failed", "error_code": code}
        return {
            "job_id": result.job_id,
            "executor": job.executor,
            "status": "published" if result.published else "stale",
            "snapshot_id": result.snapshot_id,
            "artifact_sha256": result.artifact_sha256,
            "cache_hit": result.cache_hit,
            "input_rows": result.input_rows,
            "timings_seconds": result.timings_seconds,
        }

    def _run_ai(self, job, *, lease_seconds):
        try:
            result = execute_ai_job(job, allow_paid_ai=True, lease_seconds=lease_seconds)
        except AIWorkerCancelled:
            finish_cancelled_job(job.pk, job.lease_token)
            return {"job_id": job.pk, "executor": job.executor, "status": "cancelled"}
        except AIWorkerSuperseded:
            return {"job_id": job.pk, "executor": job.executor, "status": "superseded"}
        except AIWorkerExecutionError as exc:
            if not exc.already_finalized:
                fail_job(
                    job.pk,
                    job.lease_token,
                    error_code=exc.code,
                    retryable=exc.retryable,
                    retry_delay_seconds=30 if exc.retryable else 0,
                )
            return {"job_id": job.pk, "executor": job.executor, "status": "failed", "error_code": exc.code}
        except Exception as exc:
            code = f"ai_worker_{type(exc).__name__.lower()}"[:64]
            fail_job(job.pk, job.lease_token, error_code=code, retryable=False)
            return {"job_id": job.pk, "executor": job.executor, "status": "failed", "error_code": code}
        return {
            "job_id": result.job_id,
            "executor": job.executor,
            "status": "published",
            "snapshot_id": result.snapshot_id,
            "stage_ids": result.stage_ids,
            "reused_stage_count": result.reused_stage_count,
        }

    def handle(self, *args, **options):
        external_values = (
            options.get("external_source_ref"),
            options.get("manifest"),
            options.get("mapping"),
        )
        if any(external_values) and not all(external_values):
            raise CommandError("外部資料必須同時提供 --external-source-ref、--manifest 與 --mapping")
        lease_seconds = options["lease_seconds"]
        poll_seconds = options["poll_seconds"]
        if lease_seconds < 5:
            raise CommandError("--lease-seconds 不得小於 5")
        if poll_seconds < 0.1 or poll_seconds > 300:
            raise CommandError("--poll-seconds 必須介於 0.1 與 300")
        external_inputs = {}
        if all(external_values):
            source_ref, manifest, mapping = external_values
            external_inputs[source_ref] = ExternalInputSpec(Path(manifest), Path(mapping))

        self._event(status="started", paid_ai=bool(options["allow_paid_ai"]))
        while True:
            job = claim_next_job(
                options["worker_id"],
                lease_seconds=lease_seconds,
                executor=AnalysisJob.Executor.DETERMINISTIC,
                external_source_refs=external_inputs.keys(),
            )
            if job:
                self._event(
                    **self._run_deterministic(
                        job,
                        output=options["output"],
                        external_inputs=external_inputs,
                        lease_seconds=lease_seconds,
                    )
                )
            elif options["allow_paid_ai"]:
                job = claim_next_job(
                    options["worker_id"],
                    lease_seconds=lease_seconds,
                    executor=AnalysisJob.Executor.AI,
                )
                if job:
                    self._event(**self._run_ai(job, lease_seconds=lease_seconds))
            if options["once"]:
                if not job:
                    self._event(status="idle")
                return
            if not job:
                time.sleep(poll_seconds)
