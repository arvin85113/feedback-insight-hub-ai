"""Small, lease-bound stage checkpoints; never an estimate of elapsed-time progress."""

from django.db import transaction
from django.utils import timezone

from .models import AnalysisJob


PHASES = {
    "prepare": "準備並驗證分析輸入", "statistics": "計算統計", "text": "分析評論文字",
    "assemble": "整理分析證據", "ai_statistics": "Gemini：產生統計解讀",
    "ai_text": "Gemini：產生文字洞察", "ai_synthesis": "Gemini：產生綜合解析",
    "publish": "驗證並發布結果",
}
EXECUTOR_PHASES = {
    "deterministic": {"prepare", "statistics", "text", "assemble", "publish"},
    "ai": {"prepare", "ai_statistics", "ai_text", "ai_synthesis", "publish"},
}


def report_job_progress(job_id, lease_token, *, phase, completed):
    """Only the current, uncancelled claim may update non-sensitive progress metadata.

    Four units are statistics/text/evidence/publication or three AI stages/publication.
    Completion (100%) is inferred only from the published, succeeded job status.
    """
    if phase not in PHASES or type(completed) is not int or not 0 <= completed < 4:
        raise ValueError("Invalid stage progress")
    now = timezone.now()
    with transaction.atomic():
        job = AnalysisJob.objects.select_for_update().filter(pk=job_id).first()
        if (not job or job.status != "running" or job.lease_token != lease_token
                or job.cancel_requested_at or not job.lease_expires_at or job.lease_expires_at <= now):
            return False
        if phase not in EXECUTOR_PHASES.get(job.executor, set()):
            raise ValueError("Invalid executor phase")
        manifest = dict(job.result_manifest or {})
        previous = manifest.get("_progress") or {}
        if not isinstance(previous, dict):
            previous = {}
        if (previous.get("attempt") == job.attempt_count
                and type(previous.get("completed")) is int and completed < previous["completed"]):
            return False
        manifest["_progress"] = {"version": 1, "attempt": job.attempt_count, "phase": phase,
                                 "completed": completed, "total": 4, "updated_at": now.isoformat()}
        job.result_manifest = manifest
        job.save(update_fields=("result_manifest", "updated_at"))
    return True
