import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from feedback.analysis_jobs import (
    claim_next_job,
    fail_job,
    finish_cancelled_job,
)
from feedback.analysis_worker import (
    ExternalInputSpec,
    WorkerCancelled,
    WorkerExecutionError,
    execute_deterministic_job,
)
from feedback.models import AnalysisJob


class Command(BaseCommand):
    help = "領取並執行一筆統計／文字分析工作；不呼叫 Gemini。"

    def add_arguments(self, parser):
        parser.add_argument("--worker-id", required=True, help="不含憑證的穩定 Worker 名稱")
        parser.add_argument("--output", required=True, help="本機版本化分析產物目錄")
        parser.add_argument("--lease-seconds", type=int, default=600)
        parser.add_argument("--external-source-ref")
        parser.add_argument("--manifest")
        parser.add_argument("--mapping")

    def handle(self, *args, **options):
        external_values = (
            options.get("external_source_ref"),
            options.get("manifest"),
            options.get("mapping"),
        )
        if any(external_values) and not all(external_values):
            raise CommandError("外部資料必須同時提供 --external-source-ref、--manifest 與 --mapping")
        external_inputs = {}
        if all(external_values):
            source_ref, manifest, mapping = external_values
            external_inputs[source_ref] = ExternalInputSpec(Path(manifest), Path(mapping))
        lease_seconds = options["lease_seconds"]
        if lease_seconds < 5:
            raise CommandError("--lease-seconds 不得小於 5")

        job = claim_next_job(
            options["worker_id"],
            lease_seconds=lease_seconds,
            executor=AnalysisJob.Executor.DETERMINISTIC,
            external_source_refs=external_inputs.keys(),
        )
        if job is None:
            self.stdout.write(json.dumps({"status": "idle"}, ensure_ascii=False))
            return
        try:
            result = execute_deterministic_job(
                job,
                output_root=options["output"],
                external_inputs=external_inputs,
                lease_seconds=lease_seconds,
            )
        except WorkerCancelled:
            finish_cancelled_job(job.pk, job.lease_token)
            self.stdout.write(json.dumps({"job_id": job.pk, "status": "cancelled"}, ensure_ascii=False))
            return
        except WorkerExecutionError as exc:
            fail_job(
                job.pk,
                job.lease_token,
                error_code=exc.code,
                retryable=exc.retryable,
                retry_delay_seconds=30 if exc.retryable else 0,
            )
            raise CommandError(f"analysis worker failed: {exc.code}") from exc
        except Exception as exc:
            code = f"worker_{type(exc).__name__.lower()}"[:64]
            fail_job(job.pk, job.lease_token, error_code=code, retryable=False)
            raise CommandError(f"analysis worker failed: {code}") from exc

        self.stdout.write(
            json.dumps(
                {
                    "job_id": result.job_id,
                    "status": "published" if result.published else "stale",
                    "snapshot_id": result.snapshot_id,
                    "artifact_path": str(result.artifact_path),
                    "artifact_sha256": result.artifact_sha256,
                    "cache_hit": result.cache_hit,
                    "input_rows": result.input_rows,
                    "timings_seconds": result.timings_seconds,
                },
                ensure_ascii=False,
            )
        )
