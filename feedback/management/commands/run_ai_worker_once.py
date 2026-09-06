import json

from django.core.management.base import BaseCommand, CommandError

from feedback.ai_worker import (
    AIWorkerCancelled,
    AIWorkerExecutionError,
    AIWorkerSuperseded,
    execute_ai_job,
)
from feedback.analysis_jobs import claim_next_job, fail_job, finish_cancelled_job
from feedback.models import AnalysisJob


class Command(BaseCommand):
    help = "明確授權後領取並執行一筆 Gemini AI 工作。"

    def add_arguments(self, parser):
        parser.add_argument("--worker-id", required=True, help="不含憑證的穩定 Worker 名稱")
        parser.add_argument("--lease-seconds", type=int, default=600)
        parser.add_argument(
            "--allow-paid-ai",
            action="store_true",
            help="確認本次可呼叫真實付費 Gemini API",
        )

    def handle(self, *args, **options):
        if not options["allow_paid_ai"]:
            raise CommandError("必須明確提供 --allow-paid-ai；未授權時不會領取工作")
        lease_seconds = options["lease_seconds"]
        if lease_seconds < 5:
            raise CommandError("--lease-seconds 不得小於 5")
        job = claim_next_job(
            options["worker_id"],
            lease_seconds=lease_seconds,
            executor=AnalysisJob.Executor.AI,
        )
        if job is None:
            self.stdout.write(json.dumps({"status": "idle"}, ensure_ascii=False))
            return
        try:
            result = execute_ai_job(job, allow_paid_ai=True, lease_seconds=lease_seconds)
        except AIWorkerCancelled:
            finish_cancelled_job(job.pk, job.lease_token)
            self.stdout.write(json.dumps({"job_id": job.pk, "status": "cancelled"}, ensure_ascii=False))
            return
        except AIWorkerSuperseded:
            self.stdout.write(json.dumps({"job_id": job.pk, "status": "superseded"}, ensure_ascii=False))
            return
        except AIWorkerExecutionError as exc:
            if not exc.already_finalized:
                fail_job(
                    job.pk,
                    job.lease_token,
                    error_code=exc.code,
                    retryable=exc.retryable,
                    retry_delay_seconds=30 if exc.retryable else 0,
                )
            raise CommandError(f"AI worker failed: {exc.code}") from exc
        except Exception as exc:
            code = f"ai_worker_{type(exc).__name__.lower()}"[:64]
            fail_job(job.pk, job.lease_token, error_code=code, retryable=False)
            raise CommandError(f"AI worker failed: {code}") from exc

        self.stdout.write(
            json.dumps(
                {
                    "job_id": result.job_id,
                    "status": "published",
                    "snapshot_id": result.snapshot_id,
                    "stage_ids": result.stage_ids,
                    "reused_stage_count": result.reused_stage_count,
                },
                ensure_ascii=False,
            )
        )
