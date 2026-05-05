from statistics import mean

from django.conf import settings
from django.db import models
from django.db.models import Count
from django.urls import reverse
from django.utils import timezone

from .text_pipeline import keyword_counts, tokenize_feedback


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
    survey = models.ForeignKey(Survey, on_delete=models.CASCADE, related_name="improvements")
    title = models.CharField(max_length=255)
    summary = models.TextField()
    related_category = models.CharField(max_length=100, blank=True)
    send_global_notice = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    emailed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class ImprovementDispatch(models.Model):
    improvement = models.ForeignKey(ImprovementUpdate, on_delete=models.CASCADE, related_name="dispatches")
    submission = models.ForeignKey(FeedbackSubmission, on_delete=models.CASCADE, related_name="dispatches")
    personalized_note = models.TextField(blank=True)
    sent_at = models.DateTimeField(default=timezone.now)
    is_read = models.BooleanField(default=False)

    class Meta:
        unique_together = ("improvement", "submission")


def keyword_summary(survey):
    answer_pairs = Answer.objects.filter(
        question__survey=survey,
        question__enable_keyword_tracking=True,
    ).values_list("analysis_text", "value")
    text_values = [analysis_text or value for analysis_text, value in answer_pairs if (analysis_text or value)]
    counts = keyword_counts(text_values)

    all_rules = list(survey.keyword_categories.all())

    categories = []
    for keyword, count in counts.most_common(20):
        mapping = None
        for rule in all_rules:
            if count >= rule.threshold and (
                rule.keyword == keyword or
                rule.keyword in keyword or
                keyword in rule.keyword
            ):
                mapping = rule
                break
        categories.append(
            {
                "keyword": keyword,
                "count": count,
                "category": mapping.category if mapping else "未分類",
            }
        )
    return categories


def text_analysis_summary(survey):
    answers = Answer.objects.filter(
        question__survey=survey,
        question__enable_keyword_tracking=True,
    )
    total_answers = answers.count()
    analyzed_answers = answers.exclude(analysis_text__isnull=True).exclude(analysis_text="")
    sentiment_values = list(answers.exclude(sentiment_score__isnull=True).values_list("sentiment_score", flat=True))
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
    category_map = {
        keyword: category
        for keyword, category in survey.keyword_categories.values_list("keyword", "category")
    }
    bucket = {}

    for analysis_text, value, sentiment_score in answers:
        tokens = tokenize_feedback(analysis_text or value or "")
        if not tokens:
            continue
        categories = {category_map.get(token, "未分類") for token in tokens}
        if not categories:
            categories = {"未分類"}

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
