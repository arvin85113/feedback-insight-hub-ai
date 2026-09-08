"""Authoritative analysis-source selection and immutable external versions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import AnalysisJob, ExternalDatasetVersion, Survey, SurveyAnalysisSource


class AnalysisSourceConfigurationError(RuntimeError):
    """The survey's configured input cannot safely be used by a worker."""


@dataclass(frozen=True)
class AnalysisSourceBinding:
    kind: str
    source_ref: str = ""
    source_version: str = ""
    external_version_id: int | None = None
    mapping_key: str = ""
    mapping_version: str = ""
    row_count: int = 0
    source_latest_at: datetime | None = None

    @property
    def is_external(self):
        return self.kind == AnalysisJob.SourceKind.EXTERNAL

    def job_values(self):
        return {
            "source_kind": self.kind,
            "source_ref": self.source_ref,
            "source_version": self.source_version,
        }

    def matches_job(self, job):
        return (
            job.source_kind == self.kind
            and job.source_ref == self.source_ref
            and job.source_version == self.source_version
        )


def resolve_analysis_source(survey, *, lock=False):
    """Return the configured source, with normal answers as the legacy-safe default."""

    survey_id = getattr(survey, "pk", survey)
    source = None
    if not lock and not isinstance(survey, (int, str)):
        try:
            source = survey.analysis_source
        except SurveyAnalysisSource.DoesNotExist:
            source = None
    else:
        query = SurveyAnalysisSource.objects.filter(survey_id=survey_id)
        if lock:
            # PostgreSQL rejects FOR UPDATE when select_related() introduces the
            # nullable active-version side of an outer join.  Lock the source
            # row itself, then let the version FK load separately.
            query = query.select_for_update()
        else:
            query = query.select_related("active_external_version")
        source = query.first()
    if source is None:
        return AnalysisSourceBinding(kind=AnalysisJob.SourceKind.ANSWERS)
    if source.kind == SurveyAnalysisSource.Kind.ANSWERS:
        if source.active_external_version_id:
            raise AnalysisSourceConfigurationError("問卷回覆來源不應連結外部資料版本")
        return AnalysisSourceBinding(kind=AnalysisJob.SourceKind.ANSWERS)
    if source.kind != SurveyAnalysisSource.Kind.EXTERNAL:
        raise AnalysisSourceConfigurationError("不支援的分析資料來源")
    version = source.active_external_version
    if version is None:
        raise AnalysisSourceConfigurationError("外部資料來源尚未指定作用中版本")
    if version.source_id != source.pk:
        raise AnalysisSourceConfigurationError("作用中外部資料版本不屬於此問卷")
    return AnalysisSourceBinding(
        kind=AnalysisJob.SourceKind.EXTERNAL,
        source_ref=version.source_ref,
        source_version=version.source_version,
        external_version_id=version.pk,
        mapping_key=version.mapping_key,
        mapping_version=version.mapping_version,
        row_count=version.row_count,
        source_latest_at=version.source_latest_at,
    )


def _sha256(value, field_name):
    value = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{field_name} 必須是小寫 64 字元 SHA-256")
    return value


def register_external_dataset_version(
    survey_id,
    *,
    source_ref,
    source_version,
    source_revision,
    cleaning_version,
    content_sha256,
    schema_sha256="",
    mapping_key,
    mapping_version,
    row_count,
    source_latest_at=None,
    provenance=None,
):
    """Register (or select) an immutable external version and queue fresh work.

    This stores provenance only.  It intentionally never stores a local file
    path or reads the Parquet; desktop workers resolve their own local registry.
    """

    source_ref = str(source_ref or "").strip()
    source_version = str(source_version or "").strip()
    source_revision = str(source_revision or "").strip()
    cleaning_version = str(cleaning_version or "").strip()
    mapping_key = str(mapping_key or "").strip()
    mapping_version = str(mapping_version or "").strip()
    if not all((source_ref, source_version, source_revision, cleaning_version, mapping_key, mapping_version)):
        raise ValueError("外部資料版本缺少必要來源、清理或 mapping 資訊")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", mapping_key):
        raise ValueError("mapping_key 格式不正確")
    if int(row_count) < 1:
        raise ValueError("外部資料筆數必須至少為 1")
    content_sha256 = _sha256(content_sha256, "content_sha256")
    schema_sha256 = _sha256(schema_sha256, "schema_sha256") if schema_sha256 else ""

    from .analysis_jobs import schedule_survey_analysis, suppress_analysis_scheduling

    with transaction.atomic():
        survey = Survey.objects.select_for_update().filter(pk=survey_id).first()
        if survey is None:
            raise ValueError("找不到問卷")
        with suppress_analysis_scheduling():
            source, _ = SurveyAnalysisSource.objects.select_for_update().get_or_create(
                survey=survey,
                defaults={"kind": SurveyAnalysisSource.Kind.EXTERNAL},
            )
            if source.kind != SurveyAnalysisSource.Kind.EXTERNAL:
                source.kind = SurveyAnalysisSource.Kind.EXTERNAL
                source.active_external_version = None
                source.save(update_fields=("kind", "active_external_version", "updated_at"))
            version, created = ExternalDatasetVersion.objects.get_or_create(
                source=source,
                source_ref=source_ref,
                source_version=source_version,
                defaults={
                    "source_revision": source_revision,
                    "cleaning_version": cleaning_version,
                    "content_sha256": content_sha256,
                    "schema_sha256": schema_sha256,
                    "mapping_key": mapping_key,
                    "mapping_version": mapping_version,
                    "row_count": int(row_count),
                    "source_latest_at": source_latest_at,
                    "provenance": dict(provenance or {}),
                },
            )
            if not created:
                expected = {
                    "source_revision": source_revision,
                    "cleaning_version": cleaning_version,
                    "content_sha256": content_sha256,
                    "schema_sha256": schema_sha256,
                    "mapping_key": mapping_key,
                    "mapping_version": mapping_version,
                    "row_count": int(row_count),
                    "source_latest_at": source_latest_at,
                    "provenance": dict(provenance or {}),
                }
                conflicts = [field for field, value in expected.items() if getattr(version, field) != value]
                if conflicts:
                    raise ValidationError("同一外部資料版本的來源資訊不可變更：" + "、".join(conflicts))
            changed = source.active_external_version_id != version.pk
            if changed:
                source.active_external_version = version
                source.save(update_fields=("active_external_version", "updated_at"))
        # A newly selected version is a new input and mapping configuration.
        # Re-registering the same immutable version is intentionally a no-op.
        job = schedule_survey_analysis(survey.pk, change="both") if changed else None
    return source, version, job, changed
