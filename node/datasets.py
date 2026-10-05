"""Version-bound, node-only dataset paths. Registration and queueing commit together."""

from django.conf import settings
from django.db import transaction

from feedback.analysis_sources import register_external_dataset_version
from feedback.external_dataset import ExternalDatasetInvalid, validate_external_dataset

from .models import LocalDatasetLocation


def register_local_dataset(survey_id, verified):
    if not settings.IS_NODE:
        raise ExternalDatasetInvalid("本機路徑只能登錄於節點模式")
    with transaction.atomic():
        source, version, job, changed = register_external_dataset_version(
            survey_id, **verified.registration
        )
        _, location_created = LocalDatasetLocation.objects.update_or_create(
            version=version,
            defaults={
                "manifest_path": str(verified.manifest_path),
                "mapping_path": str(verified.mapping_path),
                "manifest_sha256": verified.manifest_sha256,
                "mapping_sha256": verified.mapping_sha256,
            },
        )
        if location_created and not changed:
            from feedback.analysis_jobs import schedule_survey_analysis
            from feedback.models import AnalysisJob
            # Background definition sync may have queued a job before the files were
            # attached. If it already failed for the missing locator, queue again
            # without inventing a new input/config version. Exact retries stay no-op.
            in_flight = AnalysisJob.objects.filter(survey_id=survey_id, source_kind="external",
                source_ref=version.source_ref, source_version=version.source_version,
                executor=AnalysisJob.Executor.DETERMINISTIC,
                status__in=(AnalysisJob.Status.PENDING, AnalysisJob.Status.RUNNING)).exists()
            if not in_flight:
                job = schedule_survey_analysis(survey_id, change="none")
        return source, version, job, changed


def input_for_job(job):
    from feedback.analysis_worker import ExternalInputSpec, WorkerExecutionError

    location = LocalDatasetLocation.objects.select_related("version").filter(
        version__source__survey_id=job.survey_id,
        version__source_ref=job.source_ref,
        version__source_version=job.source_version,
    ).first()
    if location is None:
        raise WorkerExecutionError("external_location_missing")
    try:
        verified = validate_external_dataset(location.manifest_path, location.mapping_path)
        version = location.version
        if (verified.manifest_sha256 != location.manifest_sha256
                or verified.mapping_sha256 != location.mapping_sha256
                or any(getattr(version, key) != value for key, value in verified.registration.items())):
            raise ExternalDatasetInvalid("登錄版本或 mapping 已變更")
    except (ExternalDatasetInvalid, OSError) as exc:
        # Do not expose the local path or source contents in a job error/log.
        raise WorkerExecutionError("external_location_integrity_mismatch") from exc
    return ExternalInputSpec(verified.manifest_path, verified.mapping_path)
