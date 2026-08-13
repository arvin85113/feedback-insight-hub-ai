from collections import Counter
import re
from statistics import mean

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Count
from django.urls import reverse
from django.utils import timezone


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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["title"]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("feedback:survey-detail", args=[self.slug])


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
    title = models.CharField(max_length=255)
    help_text = models.CharField(max_length=255, blank=True)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    data_type = models.CharField(max_length=20, choices=DataType.choices)
    options_text = models.TextField(blank=True, help_text="每行一個選項，供單選或多選題使用。")
    is_required = models.BooleanField(default=True)
    enable_keyword_tracking = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.survey.title} - {self.title}"

    @property
    def options(self):
        return [line.strip() for line in self.options_text.splitlines() if line.strip()]


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
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-submitted_at"]

    def __str__(self):
        return f"{self.survey.title} @ {self.submitted_at:%Y-%m-%d %H:%M}"

    @property
    def display_name(self):
        if self.respondent_name:
            return self.respondent_name
        if self.user:
            return self.user.get_full_name() or self.user.username
        return "匿名填答者"


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
    send_global_notice = models.BooleanField(default=True)
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
    created_at = models.DateTimeField(auto_now_add=True)
    emailed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=("source_ai_analysis_stage", "source_ai_draft_id"),
                name="uniq_improvement_ai_draft",
            )
        ]

    def __str__(self):
        return self.title


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


class ImprovementDispatch(models.Model):
    improvement = models.ForeignKey(ImprovementUpdate, on_delete=models.CASCADE, related_name="dispatches")
    submission = models.ForeignKey(FeedbackSubmission, on_delete=models.CASCADE, related_name="dispatches")
    personalized_note = models.TextField(blank=True)
    sent_at = models.DateTimeField(default=timezone.now)
    is_read = models.BooleanField(default=False)

    class Meta:
        unique_together = ("improvement", "submission")


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
        question__enable_keyword_tracking=True,
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
    for question in survey.questions.all():
        answers = Answer.objects.filter(question=question)
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
        question__enable_keyword_tracking=True,
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
        question__enable_keyword_tracking=True,
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
