from collections import Counter
import re
from statistics import mean
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Count
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify


class SurveyCategory(models.Model):
    name = models.CharField(max_length=100, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "問卷分類"
        verbose_name_plural = "問卷分類"

    def __str__(self):
        return self.name


class Survey(models.Model):
    title = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)
    category = models.ForeignKey(
        SurveyCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="surveys",
        verbose_name="分類",
    )
    thank_you_email_enabled = models.BooleanField(default=True)
    improvement_tracking_enabled = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    analysis_enabled = models.BooleanField(default=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["title"]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("feedback:survey-detail", args=[self.slug])

    @property
    def accepts_responses(self):
        return self.is_active and self.archived_at is None


class Question(models.Model):
    class Kind(models.TextChoices):
        SHORT_TEXT = "short_text", "短文字"
        LONG_TEXT = "long_text", "長文字"
        SINGLE_CHOICE = "single_choice", "單選"
        MULTIPLE_CHOICE = "multiple_choice", "多選"
        INTEGER = "integer", "整數"
        DECIMAL = "decimal", "小數"
        SCALE = "scale", "量表"

    class DataType(models.TextChoices):
        NOMINAL = "nominal", "名目"
        ORDINAL = "ordinal", "順序"
        DISCRETE = "discrete", "離散"
        CONTINUOUS = "continuous", "連續"
        TEXT = "text", "文字"

    survey = models.ForeignKey(Survey, on_delete=models.CASCADE, related_name="questions")
    code = models.SlugField(max_length=80)
    title = models.CharField(max_length=255)
    help_text = models.CharField(max_length=255, blank=True)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    data_type = models.CharField(max_length=20, choices=DataType.choices)
    options_text = models.TextField(blank=True, help_text="每行一個選項，供單選或多選題使用。")
    is_required = models.BooleanField(default=True)
    enable_keyword_tracking = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["order", "id"]
        constraints = [
            models.UniqueConstraint(fields=("survey", "code"), name="fb_question_survey_code_uniq"),
        ]

    def __str__(self):
        return f"{self.survey.title} - {self.title}"

    def clean(self):
        super().clean()
        allowed_types = {
            self.Kind.SHORT_TEXT: {self.DataType.TEXT},
            self.Kind.LONG_TEXT: {self.DataType.TEXT},
            self.Kind.SINGLE_CHOICE: {self.DataType.NOMINAL, self.DataType.ORDINAL},
            self.Kind.MULTIPLE_CHOICE: {self.DataType.NOMINAL},
            self.Kind.INTEGER: {self.DataType.DISCRETE},
            self.Kind.DECIMAL: {self.DataType.CONTINUOUS},
            self.Kind.SCALE: {self.DataType.ORDINAL},
        }
        if self.kind in allowed_types and self.data_type not in allowed_types[self.kind]:
            raise ValidationError(
                {"data_type": f"{self.get_kind_display()} 不支援此資料型態。"}
            )
        if self.kind in {self.Kind.SINGLE_CHOICE, self.Kind.MULTIPLE_CHOICE} and not self.options:
            raise ValidationError({"options_text": "單選與多選題至少需要一個選項。"})

    @property
    def options(self):
        return [line.strip() for line in self.options_text.splitlines() if line.strip()]

    def save(self, *args, **kwargs):
        if not self.code:
            base = slugify(self.title)[:64] or "question"
            code = base
            suffix = 2
            while type(self).objects.filter(survey_id=self.survey_id, code=code).exclude(pk=self.pk).exists():
                code = f"{base[:70]}-{suffix}"
                suffix += 1
            self.code = code
        return super().save(*args, **kwargs)


class FeedbackSubmission(models.Model):
    survey = models.ForeignKey(Survey, on_delete=models.CASCADE, related_name="submissions")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="submissions",
        blank=True,
        null=True,
    )
    respondent_name = models.CharField(max_length=120, blank=True)
    respondent_email = models.EmailField(blank=True)
    consent_follow_up = models.BooleanField(default=False)
    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    submitted_at = models.DateTimeField(auto_now_add=True)
    ingested_at = models.DateTimeField(auto_now_add=True)
    is_complete = models.BooleanField(default=True)
    voided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-submitted_at"]
        indexes = [
            models.Index(fields=("survey", "is_complete", "voided_at", "submitted_at"), name="fb_sub_analysis_idx"),
        ]

    def __str__(self):
        return f"{self.survey.title} @ {self.submitted_at:%Y-%m-%d %H:%M}"

    @property
    def display_name(self):
        if self.respondent_name:
            return self.respondent_name
        if self.user:
            return self.user.get_full_name() or self.user.username
        return "匿名填答者"


class DatasetImportBatch(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "等待匯入"
        RUNNING = "running", "匯入中"
        COMPLETED = "completed", "已完成"
        FAILED = "failed", "失敗"

    survey = models.ForeignKey(
        Survey,
        on_delete=models.CASCADE,
        related_name="dataset_import_batches",
    )
    source_name = models.CharField(max_length=160)
    source_version = models.CharField(max_length=100)
    source_url = models.URLField(max_length=500, blank=True)
    license_name = models.CharField(max_length=120, blank=True)
    input_file_sha256 = models.CharField(max_length=64)
    mapping_version = models.CharField(max_length=64)
    sampling_method = models.CharField(max_length=80, default="reservoir_without_replacement")
    random_seed = models.IntegerField(default=42, null=True, blank=True)
    requested_limit = models.PositiveIntegerField(null=True, blank=True)
    read_count = models.PositiveIntegerField(default=0)
    imported_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    duplicate_count = models.PositiveIntegerField(default=0)
    conflict_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    summary = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=("survey", "-created_at"), name="fb_import_survey_idx"),
            models.Index(fields=("source_name", "source_version"), name="fb_import_source_idx"),
        ]

    def __str__(self):
        return f"{self.source_name} {self.source_version} / {self.survey}"


class ImportedSubmissionSource(models.Model):
    submission = models.OneToOneField(
        FeedbackSubmission,
        on_delete=models.CASCADE,
        related_name="imported_source",
    )
    batch = models.ForeignKey(
        DatasetImportBatch,
        on_delete=models.PROTECT,
        related_name="submission_sources",
    )
    source_namespace = models.CharField(max_length=160)
    source_record_key = models.CharField(max_length=64)
    content_sha256 = models.CharField(max_length=64)
    source_version = models.CharField(max_length=100)
    source_item_id = models.CharField(max_length=255, blank=True)
    source_timestamp = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("source_namespace", "source_record_key"),
                name="fb_import_source_record_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=("batch", "created_at"), name="fb_import_batch_idx"),
            models.Index(fields=("source_namespace", "source_version"), name="fb_import_version_idx"),
        ]

    def __str__(self):
        return f"{self.batch.source_name} / {self.submission_id}"


class Answer(models.Model):
    submission = models.ForeignKey(FeedbackSubmission, on_delete=models.CASCADE, related_name="answers")
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="answers")
    value = models.TextField()
    analysis_text = models.TextField(null=True, blank=True)
    sentiment_score = models.FloatField(null=True, blank=True)
    analysis_version = models.CharField(max_length=32, null=True, blank=True)

    class Meta:
        unique_together = ("submission", "question")

    def __str__(self):
        return f"{self.question.title}: {self.value[:30]}"


class KeywordCategory(models.Model):
    survey = models.ForeignKey(Survey, on_delete=models.CASCADE, related_name="keyword_categories")
    keyword = models.CharField(max_length=100)
    category = models.CharField(max_length=100)
    threshold = models.PositiveIntegerField(default=2)

    class Meta:
        unique_together = ("survey", "keyword")
        ordering = ["category", "keyword"]

    def __str__(self):
        return f"{self.category} / {self.keyword}"


class ImprovementUpdate(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PLANNED = "planned", "已規劃"
        IN_PROGRESS = "in_progress", "進行中"
        COMPLETED = "completed", "已完成"
        ARCHIVED = "archived", "已封存"

    class Priority(models.TextChoices):
        HIGH = "high", "高優先"
        MEDIUM = "medium", "中優先"
        LOW = "low", "低優先"

    survey = models.ForeignKey(
        Survey,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="improvements",
    )
    title = models.CharField(max_length=255)
    summary = models.TextField()
    related_category = models.CharField(max_length=100, blank=True)
    send_global_notice = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    priority = models.CharField(max_length=12, choices=Priority.choices, default=Priority.MEDIUM)
    due_date = models.DateField(null=True, blank=True)
    internal_note = models.TextField(blank=True)
    source_ai_analysis_stage = models.ForeignKey(
        "SurveyAIAnalysisStage",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_improvements",
    )
    source_ai_draft_id = models.CharField(max_length=64, null=True, blank=True)
    source_evidence_refs = models.JSONField(null=True, blank=True)
    source_ai_metadata = models.JSONField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_improvements",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_improvements",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    emailed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=("source_ai_analysis_stage", "source_ai_draft_id"),
                name="uniq_improvement_ai_draft",
            )
        ]
        indexes = [
            models.Index(fields=("survey", "status", "-updated_at"), name="fb_imp_survey_status_idx"),
            models.Index(fields=("status", "due_date"), name="fb_imp_status_due_idx"),
        ]

    def __str__(self):
        return self.title


class ImprovementStatusHistory(models.Model):
    improvement = models.ForeignKey(
        ImprovementUpdate,
        on_delete=models.CASCADE,
        related_name="status_history",
    )
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20, choices=ImprovementUpdate.Status.choices)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="improvement_status_changes",
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-changed_at", "-id"]
        indexes = [
            models.Index(fields=("improvement", "-changed_at"), name="fb_imp_history_idx"),
        ]

    def __str__(self):
        return f"{self.improvement} / {self.from_status or '-'} -> {self.to_status}"

    def get_from_status_display(self):
        return dict(ImprovementUpdate.Status.choices).get(self.from_status, self.from_status)


class SurveyAIReportSnapshot(models.Model):
    class Status(models.TextChoices):
        BUILDING = "building", "建立快照中"
        SNAPSHOT_READY = "snapshot_ready", "快照完成"
        GENERATING = "generating", "AI 產生中"
        SUCCEEDED = "succeeded", "成功"
        FAILED = "failed", "失敗"

    survey = models.ForeignKey(
        Survey,
        on_delete=models.CASCADE,
        related_name="ai_report_snapshots",
    )
    data_fingerprint = models.CharField(max_length=64)
    snapshot_schema_version = models.CharField(max_length=32)
    prompt_version = models.CharField(max_length=32)
    model_name = models.CharField(max_length=100)
    source_snapshot = models.JSONField(default=dict)
    ai_report = models.JSONField(null=True, blank=True)
    status = models.CharField(max_length=24, choices=Status.choices)
    response_count = models.PositiveIntegerField(default=0)
    analysis_coverage = models.DecimalField(max_digits=5, decimal_places=4, default=0)
    source_latest_at = models.DateTimeField(null=True, blank=True)
    generated_at = models.DateTimeField(null=True, blank=True)
    snapshot_ms = models.PositiveIntegerField(null=True, blank=True)
    generation_ms = models.PositiveIntegerField(null=True, blank=True)
    fingerprint_ms = models.PositiveIntegerField(null=True, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "feedback_survey_ai_report_snapshots"
        constraints = [
            models.UniqueConstraint(
                fields=(
                    "survey",
                    "data_fingerprint",
                    "snapshot_schema_version",
                    "prompt_version",
                    "model_name",
                ),
                name="uniq_survey_ai_report_version",
            )
        ]
        indexes = [
            models.Index(
                fields=("survey", "status", "generated_at"),
                name="fb_ai_survey_status_gen_idx",
            )
        ]

    def __str__(self):
        return f"{self.survey} / {self.status} / {self.data_fingerprint[:12]}"


class SurveyAIAnalysisStage(models.Model):
    class StageType(models.TextChoices):
        STATISTICS = "statistics", "統計分析"
        TEXT = "text", "文字洞察"
        SYNTHESIS = "synthesis", "綜合營運決策"

    class Status(models.TextChoices):
        GENERATING = "generating", "產生中"
        SUCCEEDED = "succeeded", "成功"
        FAILED = "failed", "失敗"

    snapshot = models.ForeignKey(
        SurveyAIReportSnapshot,
        on_delete=models.CASCADE,
        related_name="analysis_stages",
    )
    stage_type = models.CharField(max_length=16, choices=StageType.choices)
    status = models.CharField(max_length=16, choices=Status.choices)
    input_hash = models.CharField(max_length=64)
    schema_version = models.CharField(max_length=32)
    prompt_version = models.CharField(max_length=32)
    model_name = models.CharField(max_length=100)
    revision = models.PositiveIntegerField(default=1)
    input_manifest = models.JSONField(default=dict)
    output_json = models.JSONField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    generation_ms = models.PositiveIntegerField(null=True, blank=True)
    token_metrics = models.JSONField(default=dict)
    reused_from = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reuse_rows",
    )
    generated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "feedback_survey_ai_analysis_stages"
        constraints = [
            models.UniqueConstraint(
                fields=(
                    "snapshot",
                    "stage_type",
                    "input_hash",
                    "schema_version",
                    "prompt_version",
                    "model_name",
                    "revision",
                ),
                name="uniq_ai_stage_revision",
            )
        ]
        indexes = [
            models.Index(
                fields=("snapshot", "stage_type", "status", "created_at"),
                name="fb_ai_stage_lookup_idx",
            )
        ]

    def __str__(self):
        return f"{self.snapshot.survey} / {self.stage_type} / r{self.revision} / {self.status}"

    def save(self, *args, **kwargs):
        if self.pk:
            previous_status = type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()
            if previous_status in {self.Status.SUCCEEDED, self.Status.FAILED}:
                raise ValidationError("已完成的 AI 階段紀錄不可覆寫；重新執行必須建立新 revision。")
            if self.status not in {self.Status.GENERATING, self.Status.SUCCEEDED, self.Status.FAILED}:
                raise ValidationError("不支援的 AI 階段狀態轉換。")
        return super().save(*args, **kwargs)


class SurveyAnalysisState(models.Model):
    """Small, authoritative version and publication pointer for one survey."""

    survey = models.OneToOneField(
        Survey,
        on_delete=models.CASCADE,
        related_name="analysis_state",
    )
    input_version = models.PositiveBigIntegerField(default=0)
    config_version = models.PositiveBigIntegerField(default=0)
    pipeline_version = models.CharField(max_length=64, blank=True)
    published_snapshot = models.ForeignKey(
        SurveyAIReportSnapshot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="published_state_rows",
    )
    published_ai_stage = models.ForeignKey(
        SurveyAIAnalysisStage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="published_ai_state_rows",
    )
    published_display_payload = models.JSONField(default=dict, blank=True)
    published_ai_payload = models.JSONField(default=dict, blank=True)
    publication_manifest = models.JSONField(default=dict, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "feedback_survey_analysis_states"

    def __str__(self):
        return f"{self.survey} / input={self.input_version} / config={self.config_version}"


class SurveyAnalysisSource(models.Model):
    """The one authoritative input selected for a survey's analysis work.

    A missing row deliberately means the survey uses its normal persisted
    ``FeedbackSubmission`` / ``Answer`` rows.  External datasets are explicit
    so a desktop worker never infers its input from a survey slug or a local
    directory name.
    """

    class Kind(models.TextChoices):
        ANSWERS = "answers", "問卷回覆"
        EXTERNAL = "external", "外部資料表"

    survey = models.OneToOneField(
        Survey,
        on_delete=models.CASCADE,
        related_name="analysis_source",
    )
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.ANSWERS)
    active_external_version = models.ForeignKey(
        "ExternalDatasetVersion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="active_for_sources",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "feedback_survey_analysis_sources"

    def clean(self):
        super().clean()
        if self.kind == self.Kind.ANSWERS and self.active_external_version_id:
            raise ValidationError({"active_external_version": "問卷回覆來源不得指定外部資料版本。"})
        if self.kind == self.Kind.EXTERNAL and not self.active_external_version_id:
            raise ValidationError({"active_external_version": "外部資料來源必須指定作用中的資料版本。"})
        if (
            self.active_external_version_id
            and self.active_external_version.source_id != self.pk
        ):
            raise ValidationError({"active_external_version": "資料版本不屬於此問卷來源。"})

    def __str__(self):
        return f"{self.survey} / {self.kind}"


class ExternalDatasetVersion(models.Model):
    """Immutable provenance for a locally held external analysis dataset."""

    source = models.ForeignKey(
        SurveyAnalysisSource,
        on_delete=models.CASCADE,
        related_name="external_versions",
    )
    source_ref = models.CharField(max_length=255)
    source_version = models.CharField(max_length=255)
    source_revision = models.CharField(max_length=128)
    cleaning_version = models.CharField(max_length=100)
    content_sha256 = models.CharField(max_length=64)
    schema_sha256 = models.CharField(max_length=64, blank=True)
    mapping_key = models.SlugField(max_length=100)
    mapping_version = models.CharField(max_length=100)
    row_count = models.PositiveBigIntegerField()
    source_latest_at = models.DateTimeField(null=True, blank=True)
    provenance = models.JSONField(default=dict, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "feedback_external_dataset_versions"
        constraints = [
            models.UniqueConstraint(
                fields=("source", "source_ref", "source_version"),
                name="fb_external_dataset_version_uniq",
            )
        ]
        indexes = [
            models.Index(fields=("source_ref", "source_version"), name="fb_external_version_lookup_idx"),
        ]

    def clean(self):
        super().clean()
        for field_name in ("content_sha256", "schema_sha256"):
            value = getattr(self, field_name, "")
            if value and not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValidationError({field_name: "必須是小寫 64 字元 SHA-256。"})
        if self.row_count < 1:
            raise ValidationError({"row_count": "外部資料筆數必須至少為 1。"})

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(
                "source_id",
                "source_ref",
                "source_version",
                "source_revision",
                "cleaning_version",
                "content_sha256",
                "schema_sha256",
                "mapping_key",
                "mapping_version",
                "row_count",
                "source_latest_at",
                "provenance",
            ).first()
            immutable = tuple(previous or {})
            for field_name in immutable:
                if previous[field_name] != getattr(self, field_name):
                    raise ValidationError("外部資料版本建立後不可修改；請建立新的版本。")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.source_ref} @ {self.source_version}"


class AnalysisJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "等待處理"
        RUNNING = "running", "處理中"
        SUCCEEDED = "succeeded", "成功"
        FAILED = "failed", "失敗"
        CANCELLED = "cancelled", "已取消"

    class SourceKind(models.TextChoices):
        ANSWERS = SurveyAnalysisSource.Kind.ANSWERS, "問卷回覆"
        EXTERNAL = SurveyAnalysisSource.Kind.EXTERNAL, "外部資料表"

    class Executor(models.TextChoices):
        DETERMINISTIC = "deterministic", "統計與文字"
        AI = "ai", "AI 綜合分析"

    survey = models.ForeignKey(
        Survey,
        on_delete=models.CASCADE,
        related_name="analysis_jobs",
    )
    source_kind = models.CharField(
        max_length=16,
        choices=SourceKind.choices,
        default=SourceKind.ANSWERS,
    )
    source_ref = models.CharField(max_length=255, blank=True)
    source_version = models.CharField(max_length=255, blank=True)
    executor = models.CharField(
        max_length=16,
        choices=Executor.choices,
        default=Executor.DETERMINISTIC,
    )
    input_version = models.PositiveBigIntegerField()
    config_version = models.PositiveBigIntegerField()
    pipeline_version = models.CharField(max_length=64)
    input_fingerprint = models.CharField(max_length=64, blank=True)
    requested_stages = models.JSONField(default=list)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    worker_id = models.CharField(max_length=128, blank=True)
    lease_token = models.UUIDField(null=True, blank=True, editable=False)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=3)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    result_manifest = models.JSONField(default=dict, blank=True)
    available_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "feedback_analysis_jobs"
        ordering = ("created_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("survey", "source_kind", "source_ref", "executor"),
                condition=models.Q(status="pending"),
                name="fb_job_one_pending_source",
            )
        ]
        indexes = [
            models.Index(fields=("status", "available_at", "created_at"), name="fb_job_claim_idx"),
            models.Index(fields=("survey", "status", "-created_at"), name="fb_job_survey_idx"),
            models.Index(fields=("status", "lease_expires_at"), name="fb_job_lease_idx"),
        ]

    def __str__(self):
        return f"{self.survey} / {self.source_kind} / {self.status}"


class ImprovementNotice(models.Model):
    class AudienceType(models.TextChoices):
        GLOBAL = "global", "所有符合通知條件的顧客"
        SURVEY_RESPONDENTS = "survey_respondents", "這份問卷的符合條件填答者"

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        SENDING = "sending", "寄送中"
        SENT = "sent", "已寄送"
        PARTIALLY_SENT = "partially_sent", "部分成功"
        FAILED = "failed", "寄送失敗"

    improvement = models.ForeignKey(
        ImprovementUpdate,
        on_delete=models.PROTECT,
        related_name="notices",
    )
    subject = models.CharField(max_length=255)
    body = models.TextField()
    audience_type = models.CharField(max_length=24, choices=AudienceType.choices)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.DRAFT)
    recipient_count = models.PositiveIntegerField(default=0)
    sent_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_improvement_notices",
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="confirmed_improvement_notices",
    )
    confirmation_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    content_version = models.PositiveIntegerField(default=1)
    last_error_code = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=("improvement", "status", "-created_at"), name="fb_notice_imp_status_idx"),
            models.Index(fields=("status", "-updated_at"), name="fb_notice_status_idx"),
        ]

    def __str__(self):
        return f"{self.improvement} / {self.subject} / {self.status}"


class ImprovementDispatch(models.Model):
    class DeliveryStatus(models.TextChoices):
        PENDING = "pending", "等待寄送"
        SENDING = "sending", "寄送中"
        SENT = "sent", "已寄送"
        FAILED = "failed", "寄送失敗"

    improvement = models.ForeignKey(ImprovementUpdate, on_delete=models.CASCADE, related_name="dispatches")
    notice = models.ForeignKey(
        ImprovementNotice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dispatches",
    )
    submission = models.ForeignKey(
        FeedbackSubmission,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dispatches",
    )
    recipient_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="improvement_dispatches",
    )
    recipient_key = models.CharField(max_length=64, blank=True)
    personalized_note = models.TextField(blank=True)
    delivery_status = models.CharField(
        max_length=16,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.PENDING,
    )
    attempt_count = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    is_read = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("notice", "recipient_key"),
                condition=models.Q(notice__isnull=False),
                name="uniq_notice_recipient",
            ),
            models.UniqueConstraint(
                fields=("improvement", "submission"),
                condition=models.Q(notice__isnull=True, submission__isnull=False),
                name="uniq_legacy_imp_submission",
            ),
        ]
        indexes = [
            models.Index(fields=("notice", "delivery_status"), name="fb_dispatch_notice_idx"),
            models.Index(fields=("recipient_user", "is_read", "-sent_at"), name="fb_dispatch_user_idx"),
        ]


def tokenize_feedback(text):
    tokens = re.findall(r"[A-Za-z\u4e00-\u9fff]{2,}", (text or "").lower())
    stop_words = {"我們", "你們", "這個", "那個", "非常", "feedback", "問卷", "改善"}
    return [token for token in tokens if token not in stop_words]


def _resolve_keyword_category(keyword, *, count, rules):
    normalized = (keyword or "").strip().lower()
    if not normalized:
        return "未分類"

    best_rule = None
    best_score = None
    for rule in rules:
        if count < rule.threshold:
            continue
        rule_keyword = (rule.keyword or "").strip().lower()
        if not rule_keyword:
            continue
        if rule_keyword == normalized:
            score = (3, len(rule_keyword), rule.threshold)
        elif rule_keyword in normalized or normalized in rule_keyword:
            score = (2, len(rule_keyword), rule.threshold)
        else:
            continue
        if best_score is None or score > best_score:
            best_rule = rule
            best_score = score
    return best_rule.category if best_rule else "未分類"


def keyword_summary(survey):
    answer_pairs = Answer.objects.filter(
        question__survey=survey,
        question__is_active=True,
        question__enable_keyword_tracking=True,
        submission__is_complete=True,
        submission__voided_at__isnull=True,
    ).values_list("analysis_text", "value")

    counts = Counter()
    for analysis_text, value in answer_pairs:
        counts.update(tokenize_feedback(analysis_text or value))

    all_rules = list(survey.keyword_categories.all())

    categories = []
    for keyword, count in counts.most_common(20):
        categories.append(
            {
                "keyword": keyword,
                "count": count,
                "category": _resolve_keyword_category(keyword, count=count, rules=all_rules),
            }
        )
    return categories


def chart_summary(survey):
    rows = []
    for question in survey.questions.filter(is_active=True):
        answers = Answer.objects.filter(
            question=question,
            submission__is_complete=True,
            submission__voided_at__isnull=True,
        )
        if question.kind in {Question.Kind.INTEGER, Question.Kind.DECIMAL, Question.Kind.SCALE}:
            numeric_values = []
            for answer in answers:
                try:
                    numeric_values.append(float(answer.value))
                except ValueError:
                    continue
            if numeric_values:
                rows.append(
                    {
                        "question": question,
                        "type": "numeric",
                        "count": len(numeric_values),
                        "avg": round(mean(numeric_values), 2),
                        "min": min(numeric_values),
                        "max": max(numeric_values),
                    }
                )
        elif question.kind in {Question.Kind.SINGLE_CHOICE, Question.Kind.MULTIPLE_CHOICE}:
            counts = answers.values("value").annotate(total=Count("id")).order_by("-total")
            rows.append({"question": question, "type": "choice", "counts": counts})
    return rows


def recommend_analysis(question):
    if question.data_type == Question.DataType.CONTINUOUS:
        return "適合做平均數、標準差與趨勢檢視；若搭配名目分組題，可延伸到 t 檢定與 ANOVA。"
    if question.data_type == Question.DataType.DISCRETE:
        return "適合做計數型數值摘要，例如總數、平均次數與分布；第一版不自動進入 t 檢定或 ANOVA。"
    if question.data_type == Question.DataType.NOMINAL:
        return "適合做比例分布與交叉分析；單選名目題可作為推論統計的分組變數。"
    if question.data_type == Question.DataType.ORDINAL:
        return "適合做次數、比例與排序分布；因間距不一定相等，第一版不進入 t 檢定或 ANOVA。"
    if question.data_type == Question.DataType.TEXT:
        return "適合做關鍵字、情緒傾向與主題聚類，提取具體改善線索。"
    return "建議先確認資料尺度，再選擇描述統計或推論統計方法。"


def text_analysis_summary(survey):
    answers = Answer.objects.filter(
        question__survey=survey,
        question__is_active=True,
        question__enable_keyword_tracking=True,
        submission__is_complete=True,
        submission__voided_at__isnull=True,
    )
    total_answers = answers.count()
    analyzed_answers = answers.exclude(analysis_text__isnull=True).exclude(analysis_text="")
    sentiment_values = list(
        answers.exclude(sentiment_score__isnull=True).values_list("sentiment_score", flat=True)
    )
    sentiment_avg = round(mean(sentiment_values), 3) if sentiment_values else None
    return {
        "total_answers": total_answers,
        "analyzed_answers": analyzed_answers.count(),
        "analysis_coverage": round(analyzed_answers.count() / total_answers, 3) if total_answers else 0,
        "avg_sentiment_score": sentiment_avg,
    }


def category_sentiment_summary(survey):
    answers = Answer.objects.filter(
        question__survey=survey,
        question__is_active=True,
        question__enable_keyword_tracking=True,
        submission__is_complete=True,
        submission__voided_at__isnull=True,
    ).values_list("analysis_text", "value", "sentiment_score")
    all_rules = list(survey.keyword_categories.all())
    answer_tokens = []
    token_counts = Counter()
    for analysis_text, value, sentiment_score in answers:
        tokens = tokenize_feedback(analysis_text or value or "")
        if not tokens:
            continue
        answer_tokens.append((tokens, sentiment_score))
        token_counts.update(tokens)

    bucket = {}
    for tokens, sentiment_score in answer_tokens:
        categories = {
            _resolve_keyword_category(token, count=token_counts[token], rules=all_rules)
            for token in set(tokens)
        } or {"未分類"}

        for category in categories:
            row = bucket.setdefault(
                category,
                {"category": category, "positive": 0, "neutral": 0, "negative": 0, "total": 0},
            )
            row["total"] += 1
            if sentiment_score is None:
                row["neutral"] += 1
            elif sentiment_score > 0.1:
                row["positive"] += 1
            elif sentiment_score < -0.1:
                row["negative"] += 1
            else:
                row["neutral"] += 1

    return sorted(bucket.values(), key=lambda item: item["total"], reverse=True)
