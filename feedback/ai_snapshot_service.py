import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Max, Q
from django.db.models.functions import TruncDate
from django.urls import reverse
from django.utils import timezone

from .local_service import (
    STATISTICS_VERSION,
    build_stats_payload,
    build_text_analysis_payload,
)
from .evidence_projection import build_projection_manifests, effective_prompt_version
from .models import (
    Answer,
    FeedbackSubmission,
    ImprovementUpdate,
    KeywordCategory,
    Question,
    SurveyAIReportSnapshot,
)
from .text_pipeline import ANALYSIS_VERSION


logger = logging.getLogger(__name__)

SNAPSHOT_SCHEMA_VERSION = "1"
PROMPT_VERSION = "5"
SERIALIZED_REPORT_FIELDS = (
    "executive_summary",
    "positive_signals",
    "critical_findings",
    "statistical_findings",
    "text_insights",
    "improvement_drafts",
    "data_caveats",
)


def current_prompt_version():
    return effective_prompt_version(PROMPT_VERSION)
_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?886[-\s]?)?0?9\d{2}[-\s]?\d{3}[-\s]?\d{3}(?!\d)")


class SnapshotError(Exception):
    def __init__(self, error_code, user_message, *, status_code=400):
        super().__init__(error_code)
        self.error_code = error_code
        self.user_message = user_message
        self.status_code = status_code


class InsufficientResponsesError(SnapshotError):
    def __init__(self):
        super().__init__(
            "insufficient_responses",
            f"資料不足，至少需要 {settings.AI_REPORT_MIN_RESPONSES} 份有效回覆。",
        )


class SnapshotInProgressError(SnapshotError):
    def __init__(self):
        super().__init__("in_progress", "這份問卷的 AI 報告正在更新。", status_code=409)


class SourceChangedError(SnapshotError):
    def __init__(self):
        super().__init__(
            "source_changed",
            "建立分析資料時偵測到新回覆，請稍後重新更新。",
            status_code=409,
        )


@dataclass(frozen=True)
class FingerprintResult:
    value: str
    elapsed_ms: int
    response_count: int
    valid_response_count: int
    source_latest_at: object


@dataclass(frozen=True)
class SnapshotBuildResult:
    snapshot: SurveyAIReportSnapshot
    cache_hit: bool


def _clean_text(value):
    text = " ".join(str(value or "").split())
    text = _EMAIL_RE.sub("[已隱藏電子郵件]", text)
    return _PHONE_RE.sub("[已隱藏電話]", text)


_TEST_EVIDENCE_RE = re.compile(r"^test\.(test-\d+)\.(p_value|effect_size|statistic)$")
_STATISTIC_LABELS = {
    "pearson": "相關係數 r",
    "spearman": "等級相關係數 ρ",
    "one_way_anova": "F 統計量",
    "kruskal_wallis": "H 統計量",
    "welch_t_test": "t 統計量",
    "mann_whitney_u": "U 統計量",
    "chi_square": "卡方統計量 χ²",
}


def format_p_value(value):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "p 無資料"
    if numeric < 0.001:
        return "p < 0.001"
    formatted = f"{numeric:.3f}".rstrip("0").rstrip(".")
    return f"p = {formatted}"


def build_evidence_coverage(total, selected, excluded=None):
    if total is None or selected is None:
        return {"total": total, "selected": selected, "excluded": excluded, "message": ""}
    total = int(total)
    selected = int(selected)
    excluded = total - selected if excluded is None else int(excluded)
    if selected == total:
        message = f"AI 本次分析已涵蓋全部 {total} 筆聚合證據。"
    else:
        message = f"AI 本次分析涵蓋 {selected} / {total} 筆聚合證據；其餘證據仍保存在完整快照中。"
    return {"total": total, "selected": selected, "excluded": excluded, "message": message}


def _statistical_test_lookup(source_snapshot):
    return {
        row.get("test_ref"): row
        for row in (source_snapshot or {}).get("statistics", {}).get("statistical_tests", [])
        if row.get("test_ref")
    }


def serialize_evidence_for_display(row, source_snapshot=None):
    result = dict(row or {})
    evidence_id = str(result.get("id") or "")
    match = _TEST_EVIDENCE_RE.match(evidence_id)
    metric_type = result.get("metric_type")
    test_ref = result.get("test_ref")
    if match:
        test_ref = test_ref or match.group(1)
        metric_type = metric_type or match.group(2)

    if result.get("kind") == "keyword_frequency":
        label = re.sub(r"出現次數$", "", _clean_text(result.get("label"))).rstrip("： ")
        occurrence_count = result.get("occurrence_count", result.get("value"))
        display = f"{label}：共提及 {occurrence_count} 次"
        response_count = result.get("response_count")
        if isinstance(response_count, int) and not isinstance(response_count, bool) and response_count > 0:
            display += f"；涵蓋 {response_count} 份文字回覆"
        result["display_text"] = display
        return result

    if result.get("kind") != "statistical_test" or not metric_type:
        return result

    test = _statistical_test_lookup(source_snapshot).get(test_ref, {})
    method_key = result.get("method_key") or test.get("method_key")
    test_name = _clean_text(result.get("test_name") or test.get("test_name"))
    variables = result.get("variables") or [test.get("iv_title"), test.get("dv_title")]
    clean_variables = [_clean_text(value) for value in variables if _clean_text(value)]
    variable_label = " × ".join(clean_variables) if len(clean_variables) == 2 else ""
    context = "｜".join(value for value in (variable_label, test_name) if value)

    result.update(
        {
            "metric_type": metric_type,
            "method_key": method_key,
            "test_name": test_name,
            "variables": clean_variables,
        }
    )
    sample_size = result.get("sample_size")
    sample_suffix = (
        f"；樣本 {sample_size}"
        if isinstance(sample_size, int) and not isinstance(sample_size, bool) and sample_size > 0
        else ""
    )
    if metric_type == "p_value":
        p_value = format_p_value(result.get("value"))
        result["display_text"] = (f"{context}｜{p_value}" if context else p_value) + sample_suffix
        return result

    if metric_type == "statistic":
        metric_label = _STATISTIC_LABELS.get(method_key, "統計量")
    else:
        metric_label = _clean_text(result.get("effect_label") or test.get("effect_label") or "效果量")
    result["metric_label"] = metric_label
    if context:
        result["display_text"] = f"{context}｜{metric_label}：{result.get('value')}{sample_suffix}"
    else:
        fallback_label = _clean_text(result.get("label")) or metric_label
        result["display_text"] = f"{fallback_label}：{result.get('value')}{sample_suffix}"
    return result


def serialize_ai_report_content(content, source_snapshot=None):
    serialized = {}
    for key, value in (content or {}).items():
        if not isinstance(value, list):
            serialized[key] = value
            continue
        rows = []
        for item in value:
            if not isinstance(item, dict):
                rows.append(item)
                continue
            row = dict(item)
            if isinstance(row.get("evidence"), list):
                row["evidence"] = [
                    serialize_evidence_for_display(evidence, source_snapshot)
                    for evidence in row["evidence"]
                ]
            rows.append(row)
        serialized[key] = rows
    return serialized


def _hash_record(hasher, namespace, values):
    payload = json.dumps(
        [namespace, *values],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=lambda value: value.isoformat() if hasattr(value, "isoformat") else str(value),
    ).encode("utf-8")
    hasher.update(len(payload).to_bytes(8, "big"))
    hasher.update(payload)


def calculate_data_fingerprint(survey, *, include_improvements=True):
    started = time.perf_counter()
    chunk_size = settings.AI_REPORT_FINGERPRINT_CHUNK_SIZE
    hasher = hashlib.sha256()
    scope = FeedbackSubmission.objects.filter(survey=survey).aggregate(
        response_count=Count("id", distinct=True),
        valid_response_count=Count(
            "id",
            filter=Q(answers__value__gt=""),
            distinct=True,
        ),
        source_latest_at=Max("submitted_at"),
    )
    _hash_record(
        hasher,
        "survey",
        (
            survey.pk,
            survey.title,
            survey.slug,
            survey.description,
            survey.category_id,
            survey.is_active,
            survey.improvement_tracking_enabled,
            survey.updated_at,
            ANALYSIS_VERSION,
            STATISTICS_VERSION,
        ),
    )

    streams = [
        (
            "question",
            Question.objects.filter(survey=survey)
            .order_by("id")
            .values_list(
                "id",
                "title",
                "help_text",
                "kind",
                "data_type",
                "options_text",
                "is_required",
                "enable_keyword_tracking",
                "order",
            ),
        ),
        (
            "submission",
            FeedbackSubmission.objects.filter(survey=survey)
            .order_by("id")
            .values_list("id", "submitted_at"),
        ),
        (
            "answer",
            Answer.objects.filter(question__survey=survey)
            .order_by("id")
            .values_list(
                "id",
                "submission_id",
                "question_id",
                "value",
                "analysis_text",
                "sentiment_score",
                "analysis_version",
            ),
        ),
        (
            "keyword_rule",
            KeywordCategory.objects.filter(survey=survey)
            .order_by("id")
            .values_list("id", "keyword", "category", "threshold"),
        ),
    ]
    if include_improvements:
        streams.append(
            (
                "improvement",
                ImprovementUpdate.objects.filter(survey=survey)
                .order_by("id")
                .values_list(
                    "id",
                    "title",
                    "summary",
                    "related_category",
                    "send_global_notice",
                    "created_at",
                    "emailed_at",
                ),
            )
        )
    for namespace, queryset in streams:
        for row in queryset.iterator(chunk_size=chunk_size):
            _hash_record(hasher, namespace, row)

    elapsed_ms = round((time.perf_counter() - started) * 1000)
    result = FingerprintResult(
        value=hasher.hexdigest(),
        elapsed_ms=elapsed_ms,
        response_count=scope["response_count"],
        valid_response_count=scope["valid_response_count"],
        source_latest_at=scope["source_latest_at"],
    )
    logger.info(
        "ai_fingerprint survey_id=%s fingerprint=%s response_count=%s valid_response_count=%s fingerprint_ms=%s",
        survey.pk,
        result.value,
        result.response_count,
        result.valid_response_count,
        result.elapsed_ms,
    )
    return result


def _safe_ref(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _append_evidence(
    catalog,
    *,
    evidence_id,
    kind,
    label,
    value,
    unit=None,
    sample_size=None,
    **metadata,
):
    row = {
        "id": evidence_id,
        "kind": kind,
        "label": _clean_text(label),
        "value": value,
        "unit": unit,
        "sample_size": sample_size,
    }
    row.update({key: value for key, value in metadata.items() if value is not None})
    catalog.append(row)
    return evidence_id


def _sanitize_distribution(rows, caveats, label):
    visible = []
    suppressed_total = 0
    for row in rows or []:
        total = int(row.get("total") or 0)
        if total < settings.AI_REPORT_MIN_RESPONSES:
            suppressed_total += total
            continue
        visible.append(
            {
                "value": _clean_text(row.get("value")),
                "total": total,
                "percent": row.get("percent"),
            }
        )
    if suppressed_total:
        caveats.append(f"{label}包含小於三筆的群組，已隱藏或合併。")
        if suppressed_total >= settings.AI_REPORT_MIN_RESPONSES:
            visible.append({"value": "其他（已合併小樣本）", "total": suppressed_total, "percent": None})
    return visible


def _build_statistics_snapshot(survey, payload, evidence_catalog, caveats):
    question_refs = {
        question.pk: f"q{index}"
        for index, question in enumerate(survey.questions.order_by("order", "id"), start=1)
    }
    descriptive_results = []
    categorical_distributions = []
    for chart in payload.get("charts", []):
        question = chart.get("question")
        question_id = getattr(question, "pk", None)
        question_title = getattr(question, "title", None) or (question or {}).get("title", "未命名題目")
        question_ref = question_refs.get(question_id, f"q-{_safe_ref(question_title)}")
        chart_type = chart.get("type")
        if chart_type == "numeric":
            count = int(chart.get("count") or 0)
            if count < settings.AI_REPORT_MIN_RESPONSES:
                caveats.append(f"{_clean_text(question_title)}的有效樣本不足三筆，未提供數值摘要。")
                continue
            row = {
                "question_ref": question_ref,
                "question_title": _clean_text(question_title),
                "count": count,
                "average": chart.get("avg"),
                "median": chart.get("median"),
                "standard_deviation": chart.get("std"),
                "minimum": chart.get("min"),
                "maximum": chart.get("max"),
                "confidence_interval_95": [chart.get("ci_low"), chart.get("ci_high")],
            }
            descriptive_results.append(row)
            for key, label in (
                ("average", "平均數"),
                ("standard_deviation", "標準差"),
                ("minimum", "最小值"),
                ("maximum", "最大值"),
            ):
                if row[key] is not None:
                    _append_evidence(
                        evidence_catalog,
                        evidence_id=f"stats.{question_ref}.{key}",
                        kind="descriptive_statistic",
                        label=f"{question_title} {label}",
                        value=row[key],
                        sample_size=count,
                    )
            counts = _sanitize_distribution(chart.get("counts"), caveats, _clean_text(question_title))
            if counts:
                categorical_distributions.append(
                    {
                        "question_ref": question_ref,
                        "question_title": _clean_text(question_title),
                        "distribution_type": "numeric",
                        "counts": counts,
                    }
                )
        elif chart_type in {"category", "choice"}:
            counts = _sanitize_distribution(chart.get("counts"), caveats, _clean_text(question_title))
            categorical_distributions.append(
                {
                    "question_ref": question_ref,
                    "question_title": _clean_text(question_title),
                    "distribution_type": "category",
                    "counts": counts,
                }
            )
            for item in counts:
                if item["value"].startswith("其他（"):
                    continue
                evidence_id = f"distribution.{question_ref}.{_safe_ref(item['value'])}"
                _append_evidence(
                    evidence_catalog,
                    evidence_id=evidence_id,
                    kind="categorical_distribution",
                    label=f"{question_title}：{item['value']}",
                    value=item["total"],
                    unit="responses",
                    sample_size=item["total"],
                )

    statistical_tests = []
    for index, result in enumerate(payload.get("inferential_analysis", []), start=1):
        if result.get("skipped_reason"):
            statistical_tests.append(
                {
                    "test_ref": f"test-{index}",
                    "test_name": result.get("test_name") or result.get("method_key") or "未執行檢定",
                    "variables": [_clean_text(result.get("iv_title")), _clean_text(result.get("dv_title"))],
                    "skipped_reason": _clean_text(result.get("skipped_reason")),
                }
            )
            continue
        groups = result.get("groups") or []
        if any(int(group.get("count") or 0) < settings.AI_REPORT_MIN_RESPONSES for group in groups):
            caveats.append(f"{_clean_text(result.get('test_name'))}包含小樣本群組，未納入 AI 報告。")
            continue
        safe_result = {
            key: value
            for key, value in result.items()
            if key
            in {
                "analysis_family",
                "method_key",
                "test_name",
                "iv_title",
                "dv_title",
                "statistic",
                "p_value",
                "effect_size",
                "effect_label",
                "degrees_of_freedom",
                "is_significant",
                "warning",
                "insight",
            }
        }
        safe_result["test_ref"] = f"test-{index}"
        safe_result["iv_title"] = _clean_text(safe_result.get("iv_title"))
        safe_result["dv_title"] = _clean_text(safe_result.get("dv_title"))
        safe_result["insight"] = _clean_text(safe_result.get("insight"))
        safe_result["groups"] = [
            {
                "value": _clean_text(group.get("value")),
                "count": group.get("count"),
                "average_or_median": group.get("avg"),
            }
            for group in groups
        ]
        statistical_tests.append(safe_result)
        variables = [safe_result.get("iv_title"), safe_result.get("dv_title")]
        variable_label = " × ".join(value for value in variables if value)
        metric_labels = {
            "p_value": "p-value",
            "effect_size": safe_result.get("effect_label") or "效果量",
            "statistic": _STATISTIC_LABELS.get(safe_result.get("method_key"), "統計量"),
        }
        for key, label in metric_labels.items():
            if safe_result.get(key) is not None:
                _append_evidence(
                    evidence_catalog,
                    evidence_id=f"test.test-{index}.{key}",
                    kind="statistical_test",
                    label="｜".join(
                        value
                        for value in (variable_label, safe_result.get("test_name"), label)
                        if value
                    ),
                    value=safe_result[key],
                    sample_size=sum(int(group.get("count") or 0) for group in groups) or None,
                    test_ref=f"test-{index}",
                    metric_type=key,
                    method_key=safe_result.get("method_key"),
                    test_name=safe_result.get("test_name"),
                    variables=variables,
                    effect_label=safe_result.get("effect_label"),
                )
    return {
        "descriptive_results": descriptive_results,
        "categorical_distributions": categorical_distributions,
        "statistical_tests": statistical_tests,
    }


def _build_text_snapshot(payload, evidence_catalog, caveats):
    summary = payload.get("summary") or {}
    keywords = []
    for item in payload.get("keywords", []):
        count = int(item.get("count") or 0)
        if count < settings.AI_REPORT_MIN_RESPONSES:
            continue
        row = {
            "keyword": _clean_text(item.get("keyword")),
            "count": count,
            "category": _clean_text(item.get("category")),
            "response_count": item.get("response_count"),
        }
        keywords.append(row)
        _append_evidence(
            evidence_catalog,
            evidence_id=f"keyword.{_safe_ref((row['category'], row['keyword']))}",
            kind="keyword_frequency",
            label=f"關鍵字「{row['keyword']}」出現次數",
            value=count,
            unit="occurrences",
            occurrence_count=count,
            response_count=row["response_count"],
        )

    category_sentiments = []
    for item in payload.get("category_sentiments", []):
        cells = [int(item.get(key) or 0) for key in ("positive", "neutral", "negative")]
        nonzero_cells = [value for value in cells if value]
        if int(item.get("total") or 0) < settings.AI_REPORT_MIN_RESPONSES or any(
            value < settings.AI_REPORT_MIN_RESPONSES for value in nonzero_cells
        ):
            caveats.append(f"{_clean_text(item.get('category'))}的情緒分布含小樣本，已隱藏。")
            continue
        row = {
            "category": _clean_text(item.get("category")),
            "positive": cells[0],
            "neutral": cells[1],
            "negative": cells[2],
            "total": int(item.get("total") or 0),
        }
        category_sentiments.append(row)
        for key in ("positive", "neutral", "negative"):
            if row[key]:
                _append_evidence(
                    evidence_catalog,
                    evidence_id=f"sentiment.{_safe_ref(row['category'])}.{key}",
                    kind="category_sentiment",
                    label=f"{row['category']} {key}",
                    value=row[key],
                    unit="responses",
                    sample_size=row["total"],
                )
    return {
        "keywords": keywords,
        "category_sentiments": category_sentiments,
        "coverage": {
            "total_answers": summary.get("total_answers", 0),
            "analyzed_answers": summary.get("analyzed_answers", 0),
            "ratio": summary.get("analysis_coverage", 0),
            "average_sentiment_score": summary.get("avg_sentiment_score"),
        },
    }


def _build_anonymous_snapshot(survey, fingerprint):
    generated_at = timezone.now()
    caveats = []
    evidence_catalog = []
    _append_evidence(
        evidence_catalog,
        evidence_id="survey.valid_response_count",
        kind="survey_coverage",
        label="有效回覆數",
        value=fingerprint.valid_response_count,
        unit="responses",
        sample_size=fingerprint.valid_response_count,
    )
    stats_payload = build_stats_payload(survey)
    text_payload = build_text_analysis_payload(survey)
    statistics = _build_statistics_snapshot(survey, stats_payload, evidence_catalog, caveats)
    text_analysis = _build_text_snapshot(text_payload, evidence_catalog, caveats)
    text_coverage = text_analysis["coverage"]
    if text_coverage["total_answers"]:
        _append_evidence(
            evidence_catalog,
            evidence_id="text.analysis_coverage",
            kind="analysis_coverage",
            label="文字分析覆蓋率",
            value=round(float(text_coverage["ratio"]) * 100, 1),
            unit="percent",
            sample_size=int(text_coverage["total_answers"]),
        )

    trend_start = generated_at - timedelta(days=6)
    trend_rows = (
        FeedbackSubmission.objects.filter(survey=survey, submitted_at__gte=trend_start)
        .annotate(day=TruncDate("submitted_at"))
        .values("day")
        .annotate(total=Count("id"))
        .order_by("day")
    )
    trend_by_day = {row["day"]: row["total"] for row in trend_rows}
    response_trend = []
    for offset in range(7):
        day = (trend_start + timedelta(days=offset)).date()
        total = int(trend_by_day.get(day, 0))
        response_trend.append(
            {
                "date": day.isoformat(),
                "count": total if total >= settings.AI_REPORT_MIN_RESPONSES else None,
                "status": "available" if total >= settings.AI_REPORT_MIN_RESPONSES else "insufficient_sample",
            }
        )

    improvements = [
        {
            "ref": f"existing-{index}",
            "title": _clean_text(title),
            "summary": _clean_text(summary),
            "related_category": _clean_text(category),
            "notification_status": "notified" if emailed_at else "not_notified",
            "created_date": created_at.date().isoformat(),
        }
        for index, (title, summary, category, created_at, emailed_at) in enumerate(
            ImprovementUpdate.objects.filter(survey=survey)
            .order_by("created_at", "id")
            .values_list("title", "summary", "related_category", "created_at", "emailed_at")
            .iterator(chunk_size=settings.AI_REPORT_FINGERPRINT_CHUNK_SIZE),
            start=1,
        )
    ]
    coverage = text_analysis["coverage"].get("ratio") or 0
    source_snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "data_fingerprint": fingerprint.value,
        "data_scope": {
            "survey_slug": survey.slug,
            "survey_title": _clean_text(survey.title),
            "response_count": fingerprint.response_count,
            "valid_response_count": fingerprint.valid_response_count,
            "source_latest_date": (
                fingerprint.source_latest_at.date().isoformat() if fingerprint.source_latest_at else None
            ),
            "analysis_coverage": coverage,
            "text_analysis_version": ANALYSIS_VERSION,
            "statistics_version": STATISTICS_VERSION,
        },
        "dashboard_metrics": {
            "response_count": fingerprint.response_count,
            "valid_response_count": fingerprint.valid_response_count,
            "improvement_count": len(improvements),
        },
        "response_trend": response_trend,
        "statistics": statistics,
        "text_analysis": text_analysis,
        "existing_improvements": improvements,
        "evidence_catalog": evidence_catalog,
        "data_caveats": list(dict.fromkeys(caveats)),
    }
    source_snapshot["evidence_projection"] = build_projection_manifests(source_snapshot)
    return source_snapshot


def _claim_snapshot_row(survey, fingerprint):
    lookup = {
        "survey": survey,
        "data_fingerprint": fingerprint.value,
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "prompt_version": current_prompt_version(),
        "model_name": settings.GEMINI_MODEL,
    }
    try:
        with transaction.atomic():
            snapshot, created = SurveyAIReportSnapshot.objects.get_or_create(
                **lookup,
                defaults={
                    "status": SurveyAIReportSnapshot.Status.BUILDING,
                    "response_count": fingerprint.valid_response_count,
                    "source_latest_at": fingerprint.source_latest_at,
                    "fingerprint_ms": fingerprint.elapsed_ms,
                    "attempt_count": 1,
                },
            )
    except IntegrityError:
        snapshot = SurveyAIReportSnapshot.objects.get(**lookup)
        created = False
    if created:
        return snapshot, True
    if snapshot.status == SurveyAIReportSnapshot.Status.SUCCEEDED:
        return snapshot, False
    if snapshot.source_snapshot.get("data_fingerprint") == fingerprint.value:
        if snapshot.status in {
            SurveyAIReportSnapshot.Status.SNAPSHOT_READY,
            SurveyAIReportSnapshot.Status.FAILED,
        }:
            SurveyAIReportSnapshot.objects.filter(pk=snapshot.pk).update(
                status=SurveyAIReportSnapshot.Status.SNAPSHOT_READY,
                error_code="",
                updated_at=timezone.now(),
            )
            snapshot.refresh_from_db()
            return snapshot, False

    stale_before = timezone.now() - timedelta(seconds=max(settings.GEMINI_TIMEOUT_SECONDS * 2 + 10, 100))
    if snapshot.status in {
        SurveyAIReportSnapshot.Status.BUILDING,
        SurveyAIReportSnapshot.Status.GENERATING,
    } and snapshot.updated_at >= stale_before:
        raise SnapshotInProgressError
    claimed = SurveyAIReportSnapshot.objects.filter(
        pk=snapshot.pk,
        status=snapshot.status,
        updated_at=snapshot.updated_at,
    ).update(
        status=SurveyAIReportSnapshot.Status.BUILDING,
        source_snapshot={},
        ai_report=None,
        error_code="",
        attempt_count=F("attempt_count") + 1,
        updated_at=timezone.now(),
    )
    if not claimed:
        raise SnapshotInProgressError
    snapshot.refresh_from_db()
    return snapshot, True


def build_or_reuse_snapshot(survey):
    for attempt in range(2):
        fingerprint_a = calculate_data_fingerprint(survey)
        if fingerprint_a.valid_response_count < settings.AI_REPORT_MIN_RESPONSES:
            raise InsufficientResponsesError
        snapshot, should_build = _claim_snapshot_row(survey, fingerprint_a)
        if not should_build:
            return SnapshotBuildResult(snapshot=snapshot, cache_hit=True)

        started = time.perf_counter()
        try:
            source_snapshot = _build_anonymous_snapshot(survey, fingerprint_a)
            fingerprint_b = calculate_data_fingerprint(survey)
            if fingerprint_a.value != fingerprint_b.value:
                updated = SurveyAIReportSnapshot.objects.filter(
                    pk=snapshot.pk,
                    status=SurveyAIReportSnapshot.Status.BUILDING,
                    attempt_count=snapshot.attempt_count,
                ).update(
                    status=SurveyAIReportSnapshot.Status.FAILED,
                    error_code="source_changed",
                    updated_at=timezone.now(),
                )
                if not updated:
                    raise SnapshotInProgressError
                if attempt == 0:
                    continue
                raise SourceChangedError
            snapshot_ms = round((time.perf_counter() - started) * 1000)
            updated = SurveyAIReportSnapshot.objects.filter(
                pk=snapshot.pk,
                status=SurveyAIReportSnapshot.Status.BUILDING,
                attempt_count=snapshot.attempt_count,
            ).update(
                source_snapshot=source_snapshot,
                status=SurveyAIReportSnapshot.Status.SNAPSHOT_READY,
                response_count=fingerprint_b.valid_response_count,
                analysis_coverage=source_snapshot["data_scope"]["analysis_coverage"],
                source_latest_at=fingerprint_b.source_latest_at,
                snapshot_ms=snapshot_ms,
                fingerprint_ms=fingerprint_a.elapsed_ms + fingerprint_b.elapsed_ms,
                error_code="",
                updated_at=timezone.now(),
            )
            if not updated:
                raise SnapshotInProgressError
            snapshot.refresh_from_db()
            logger.info(
                "ai_snapshot_ready snapshot_id=%s survey_id=%s fingerprint=%s response_count=%s snapshot_ms=%s",
                snapshot.pk,
                survey.pk,
                snapshot.data_fingerprint,
                snapshot.response_count,
                snapshot.snapshot_ms,
            )
            return SnapshotBuildResult(snapshot=snapshot, cache_hit=False)
        except SnapshotError:
            raise
        except Exception as exc:
            SurveyAIReportSnapshot.objects.filter(
                pk=snapshot.pk,
                status=SurveyAIReportSnapshot.Status.BUILDING,
                attempt_count=snapshot.attempt_count,
            ).update(
                status=SurveyAIReportSnapshot.Status.FAILED,
                error_code="snapshot_failed",
                updated_at=timezone.now(),
            )
            logger.warning(
                "ai_snapshot_failed snapshot_id=%s survey_id=%s exception_class=%s",
                snapshot.pk,
                survey.pk,
                type(exc).__name__,
            )
            raise SnapshotError("snapshot_failed", "匿名分析資料建立失敗，既有報告不受影響。") from exc
    raise SourceChangedError


def get_report_status(survey):
    fingerprint = calculate_data_fingerprint(survey)
    prompt_version = current_prompt_version()
    latest = (
        SurveyAIReportSnapshot.objects.filter(
            survey=survey,
            status=SurveyAIReportSnapshot.Status.SUCCEEDED,
        )
        .order_by("-generated_at", "-id")
        .first()
    )
    current_key = (
        fingerprint.value,
        SNAPSHOT_SCHEMA_VERSION,
        prompt_version,
        settings.GEMINI_MODEL,
    )
    latest_key = None
    if latest:
        latest_key = (
            latest.data_fingerprint,
            latest.snapshot_schema_version,
            latest.prompt_version,
            latest.model_name,
        )
    is_current = latest_key == current_key
    has_enough_data = fingerprint.valid_response_count >= settings.AI_REPORT_MIN_RESPONSES
    latest_attempt = (
        SurveyAIReportSnapshot.objects.filter(
            survey=survey,
            data_fingerprint=fingerprint.value,
            snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
            prompt_version=prompt_version,
            model_name=settings.GEMINI_MODEL,
        )
        .order_by("-updated_at", "-id")
        .first()
    )
    latest_attempt_status = latest_attempt.status if latest_attempt else "not_started"
    latest_error_code = latest_attempt.error_code if latest_attempt else ""
    error_messages = {
        "authentication_error": "AI API 金鑰驗證失敗。",
        "forbidden": "AI API 金鑰沒有模型使用權限。",
        "model_not_found": "找不到設定的 Gemini 模型。",
        "rate_limited": "AI 服務請求過多或額度不足。",
        "timeout": "AI 產生報告逾時。",
        "server_unavailable": "AI 服務目前無法完成請求。",
        "output_truncated": "AI 報告輸出未完成。",
        "empty_response": "AI 服務沒有回傳內容。",
        "schema_invalid": "AI 報告格式未通過驗證。",
        "provider_error": "AI 服務暫時無法使用。",
    }
    return {
        "survey": {
            "slug": survey.slug,
            "title": survey.title,
            "response_count": fingerprint.response_count,
            "valid_response_count": fingerprint.valid_response_count,
        },
        "freshness": {
            "is_current": is_current,
            "has_new_data": bool(latest and not is_current),
            "has_enough_data": has_enough_data,
            "latest_analysis_incomplete": has_enough_data and not is_current,
            "latest_ai_status": latest_attempt_status,
            "latest_error_code": latest_error_code,
            "latest_error_message": error_messages.get(latest_error_code, ""),
            "minimum_responses": settings.AI_REPORT_MIN_RESPONSES,
            "fingerprint_ms": fingerprint.elapsed_ms,
        },
        "report": serialize_report(latest, is_current=is_current, cache_hit=bool(latest)) if latest else None,
    }


def serialize_report(snapshot, *, is_current, cache_hit):
    report = snapshot.ai_report or {}
    generation = report.get("_generation") or {}
    total_evidence = generation.get("total_evidence_count")
    selected_evidence = generation.get("selected_evidence_count")
    draft_urls = {
        draft.get("draft_id"): reverse(
            "feedback:ai-improvement-draft",
            args=[snapshot.survey.slug, snapshot.pk, draft.get("draft_id")],
        )
        for draft in report.get("improvement_drafts", [])
        if draft.get("draft_id")
    }
    return {
        "snapshot_id": snapshot.pk,
        "survey_slug": snapshot.survey.slug,
        "survey_title": snapshot.survey.title,
        "status": snapshot.status,
        "model": snapshot.model_name,
        "generated_at": snapshot.generated_at.isoformat() if snapshot.generated_at else None,
        "source_latest_at": snapshot.source_latest_at.isoformat() if snapshot.source_latest_at else None,
        "response_count": snapshot.response_count,
        "analysis_coverage": float(snapshot.analysis_coverage),
        "is_current": is_current,
        "cache_hit": cache_hit,
        "generation_ms": snapshot.generation_ms,
        "generation_profile": generation.get("profile", "standard"),
        "evidence_coverage": build_evidence_coverage(
            total_evidence,
            selected_evidence,
            generation.get("excluded_evidence_count"),
        ),
        "content": serialize_ai_report_content(
            {key: report.get(key) for key in SERIALIZED_REPORT_FIELDS if key in report},
            snapshot.source_snapshot,
        ),
        "draft_urls": draft_urls,
    }
