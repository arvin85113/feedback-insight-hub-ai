from datetime import timedelta

from django.db.models import Count, Max
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import (
    Answer,
    FeedbackSubmission,
    ImprovementDispatch,
    ImprovementUpdate,
    Question,
    Survey,
    chart_summary,
    keyword_summary,
    recommend_analysis,
    category_sentiment_summary,
    text_analysis_summary,
)
from .text_pipeline import ANALYSIS_VERSION, build_analysis_text, estimate_sentiment_score

_STATS_PAYLOAD_CACHE = {}
_STATS_PAYLOAD_CACHE_MAX_SIZE = 16


def format_payload_date(value):
    return f"{value.year}/{value.month}/{value.day}"


def format_payload_datetime(value):
    return f"{format_payload_date(value)} {value:%H:%M}"


def serialize_survey(survey, response_total=None):
    return {
        "id": survey.id,
        "title": survey.title,
        "slug": survey.slug,
        "description": survey.description,
        "improvement_tracking_enabled": survey.improvement_tracking_enabled,
        "thank_you_email_enabled": survey.thank_you_email_enabled,
        "questions": {"count": survey.questions.count()},
        "submissions": {"count": response_total if response_total is not None else survey.submissions.count()},
    }


def serialize_submission(submission, answer_count=None):
    category = submission.survey.category
    return {
        "id": submission.id,
        "submitted_at": submission.submitted_at.isoformat(),
        "submitted_date": format_payload_date(submission.submitted_at),
        "submitted_datetime": format_payload_datetime(submission.submitted_at),
        "consent_follow_up": submission.consent_follow_up,
        "respondent_email": submission.respondent_email,
        "display_name": submission.display_name,
        "survey": {
            "id": submission.survey.id,
            "title": submission.survey.title,
            "slug": submission.survey.slug,
            "category": {"id": category.id, "name": category.name} if category else None,
        },
        "answers": {"count": answer_count if answer_count is not None else submission.answers.count()},
    }


def serialize_notice(notice):
    return {
        "id": notice.id,
        "sent_at": notice.sent_at.isoformat(),
        "personalized_note": notice.personalized_note,
        "is_read": notice.is_read,
        "submission": {
            "id": notice.submission.id,
            "survey": {
                "id": notice.submission.survey.id,
                "title": notice.submission.survey.title,
                "slug": notice.submission.survey.slug,
            },
        },
        "improvement": {
            "id": notice.improvement.id,
            "title": notice.improvement.title,
            "summary": notice.improvement.summary,
        },
    }


def build_submission_preview(submission):
    answers = sorted(
        submission.answers.all(),
        key=lambda answer: (answer.question.order, answer.question.id),
    )
    for answer in answers:
        value = (answer.value or "").strip()
        if not value:
            continue
        if len(value) > 42:
            value = f"{value[:42]}..."
        return f"{answer.question.title}：{value}"
    return ""


def get_home_payload():
    surveys = Survey.objects.filter(is_active=True).prefetch_related("questions", "submissions")
    total_surveys = surveys.count()
    total_responses = FeedbackSubmission.objects.count()
    total_improvements = ImprovementUpdate.objects.count()
    return {
        "surveys": [serialize_survey(survey, survey.submissions.count()) for survey in surveys[:6]],
        "active_survey_count": total_surveys,
        "response_count": total_responses,
        "improvement_count": total_improvements,
        "managed_clients": max(12, total_surveys * 3 or 12),
        "response_velocity": round(total_responses / total_surveys, 1) if total_surveys else 0,
    }


def get_customer_home_payload(user):
    submissions = list(
        FeedbackSubmission.objects.filter(user=user)
        .select_related("survey", "survey__category")
        .prefetch_related("answers__question")
        .order_by("-submitted_at")
    )
    notices = list(
        ImprovementDispatch.objects.filter(submission__user=user)
        .select_related("improvement", "submission", "submission__survey")
        .order_by("-sent_at")
    )
    notices_by_submission = {}
    for notice in notices:
        notices_by_submission.setdefault(notice.submission_id, notice)

    submission_rows = []
    for submission in submissions[:8]:
        related_notice = notices_by_submission.get(submission.id)
        answer_count = submission.answers.count()
        submission_rows.append(
            {
                "submission": serialize_submission(submission, answer_count),
                "answer_count": answer_count,
                "answer_preview": build_submission_preview(submission),
                "latest_notice": serialize_notice(related_notice) if related_notice else None,
            }
        )
    return {
        "submissions": [serialize_submission(s, s.answers.count()) for s in submissions],
        "notices": [serialize_notice(n) for n in notices[:10]],
        "submission_rows": submission_rows,
        "submission_count": len(submissions),
        "active_follow_up_count": sum(1 for s in submissions if s.consent_follow_up),
        "latest_submission": serialize_submission(submissions[0], submissions[0].answers.count()) if submissions else None,
        "latest_notice": serialize_notice(notices[0]) if notices else None,
        "subscribed_survey_count": len({s.survey_id for s in submissions}),
    }


def get_customer_notifications_payload(user):
    notices = (
        ImprovementDispatch.objects.filter(submission__user=user)
        .select_related("improvement", "submission", "submission__survey")
        .order_by("-sent_at")
    )
    return {
        "notices": [serialize_notice(notice) for notice in notices],
        "notice_count": notices.count(),
        "unread_count": notices.filter(is_read=False).count(),
        "latest_notice": serialize_notice(notices.first()) if notices.exists() else None,
    }


def get_dashboard_payload():
    improvements = ImprovementUpdate.objects.select_related("survey").all()
    last_week = timezone.now() - timedelta(days=6)

    daily_qs = (
        FeedbackSubmission.objects.filter(submitted_at__gte=last_week)
        .annotate(day=TruncDate("submitted_at"))
        .values("day")
        .annotate(total=Count("id"))
    )
    counts_by_day = {row["day"]: row["total"] for row in daily_qs}
    daily_counts = [
        {
            "label": (last_week + timedelta(days=i)).date().strftime("%m/%d"),
            "total": counts_by_day.get((last_week + timedelta(days=i)).date(), 0),
        }
        for i in range(7)
    ]

    total_surveys = Survey.objects.count()
    total_submissions = FeedbackSubmission.objects.count()
    total_improvements = improvements.count()
    avg_responses = round(total_submissions / total_surveys, 1) if total_surveys else 0
    top_surveys = list(Survey.objects.annotate(sub_count=Count("submissions")).order_by("-sub_count")[:5])
    recent_responses = FeedbackSubmission.objects.select_related("survey", "user").order_by("-submitted_at")[:6]
    latest_improvements = improvements[:5]
    active_surveys = Survey.objects.filter(is_active=True).count()
    emailed_improvements = improvements.filter(emailed_at__isnull=False).count()

    insights = [
        {
            "title": "問卷活躍度",
            "body": f"目前共有 {active_surveys} 份啟用中的問卷，平均每份問卷 {avg_responses} 份回覆。",
            "tone": "neutral",
        },
        {
            "title": "顧客通知閉環",
            "body": f"已有 {emailed_improvements} 則改善項目完成通知寄送，可持續強化顧客信任。",
            "tone": "positive" if emailed_improvements else "neutral",
        },
        {
            "title": "優先關注目標",
            "body": f"{top_surveys[0].title if top_surveys else '目前尚無熱門問卷'} 是最適合先做深度分析與改善追蹤的入口。",
            "tone": "attention" if top_surveys else "neutral",
        },
    ]
    action_items = [
        {
            "title": "建立新問卷",
            "meta": "新增題組、整理題目流程與通知規則。",
            "url": "/dashboard/forms/new/",
            "url_label": "開始建立",
        },
        {
            "title": "查看統計總覽",
            "meta": "檢查連續變數、選項分布與可用分析方法。",
            "url": "/dashboard/stats/",
            "url_label": "前往分析",
        },
        {
            "title": "追蹤改善通知",
            "meta": "整理已建立的改善項目與通知派送情況。",
            "url": "/dashboard/notices/",
            "url_label": "查看通知",
        },
    ]

    return {
        "metrics": [
            {
                "label": "問卷總數",
                "value": total_surveys,
                "hint": f"{active_surveys} 份目前啟用",
                "accent": "blue",
            },
            {
                "label": "有效回覆",
                "value": total_submissions,
                "hint": "累積收到的顧客回應量",
                "accent": "violet",
            },
            {
                "label": "改善項目",
                "value": total_improvements,
                "hint": f"{emailed_improvements} 則已完成通知",
                "accent": "green",
            },
            {
                "label": "平均回覆量",
                "value": avg_responses,
                "hint": "每份問卷平均回覆數",
                "accent": "amber",
            },
        ],
        "daily_counts": daily_counts,
        "top_surveys": [serialize_survey(survey, survey.sub_count) for survey in top_surveys],
        "recent_responses": [serialize_submission(item) for item in recent_responses],
        "latest_improvements": [
            {
                "id": item.id,
                "title": item.title,
                "summary": item.summary,
                "emailed_at": item.emailed_at.isoformat() if item.emailed_at else None,
                "survey": {"id": item.survey.id, "title": item.survey.title, "slug": item.survey.slug},
            }
            for item in latest_improvements
        ],
        "insights": insights,
        "action_items": action_items,
    }


def _round_or_none(value, digits=2):
    if value != value:
        return None
    return round(float(value), digits)


def get_survey_pandas_stats(survey):
    try:
        import pandas as pd
        from scipy import stats
    except ImportError as exc:
        return {
            "charts": [],
            "inferential_analysis": [
                {
                    "skipped_reason": f"統計套件尚未安裝：{exc.name}",
                }
            ],
        }

    questions = list(survey.questions.order_by("order", "id"))
    answer_rows = list(Answer.objects.filter(question__survey=survey).values("submission_id", "question_id", "value"))
    if not questions or not answer_rows:
        return {"charts": [], "inferential_analysis": []}

    records = {}
    for row in answer_rows:
        records.setdefault(row["submission_id"], {})[f"Q_{row['question_id']}"] = row["value"]
    df = pd.DataFrame(list(records.values()))
    question_by_col = {f"Q_{question.id}": question for question in questions}

    charts = []
    numeric_columns = {}
    nominal_columns = {}
    ordinal_columns = {}

    def build_result(left_col, right_col, *, family, method_key=None):
        return {
            "analysis_family": family,
            "method_key": method_key,
            "iv_title": question_by_col[left_col].title,
            "dv_title": question_by_col[right_col].title,
        }

    def clean_category_series(col):
        return df[col].astype(str).str.strip().replace("", pd.NA)

    def encode_ordinal(question, col):
        ordered_options = question.options
        if not ordered_options:
            return None, "順序題缺少 options_text，無法安全轉成排序分數"
        rank_map = {option: idx + 1 for idx, option in enumerate(ordered_options)}
        encoded = clean_category_series(col).map(rank_map)
        return encoded, None

    def mean_confidence_interval(series):
        count = int(series.count())
        if count < 2:
            return None, None
        sem = stats.sem(series)
        if sem != sem:
            return None, None
        margin = sem * stats.t.ppf(0.975, count - 1)
        mean = series.mean()
        return _round_or_none(mean - margin), _round_or_none(mean + margin)

    def cohens_d(first, second):
        first_series = pd.Series(first)
        second_series = pd.Series(second)
        if len(first_series) < 2 or len(second_series) < 2:
            return None
        pooled_variance = (
            ((len(first_series) - 1) * first_series.var(ddof=1))
            + ((len(second_series) - 1) * second_series.var(ddof=1))
        ) / (len(first_series) + len(second_series) - 2)
        if pooled_variance <= 0:
            return None
        return _round_or_none((first_series.mean() - second_series.mean()) / (pooled_variance ** 0.5), 4)

    def eta_squared(groups_data):
        all_values = pd.Series([value for values in groups_data for value in values])
        if all_values.empty:
            return None
        grand_mean = all_values.mean()
        ss_between = sum(len(values) * ((pd.Series(values).mean() - grand_mean) ** 2) for values in groups_data)
        ss_total = sum((value - grand_mean) ** 2 for value in all_values)
        if ss_total <= 0:
            return None
        return _round_or_none(ss_between / ss_total, 4)

    for question in questions:
        col = f"Q_{question.id}"
        if col not in df.columns:
            continue

        series = df[col].dropna()
        if series.empty:
            continue

        if question.data_type in {Question.DataType.CONTINUOUS, Question.DataType.DISCRETE}:
            numeric_series = pd.to_numeric(series, errors="coerce").dropna()
            if numeric_series.empty:
                continue
            numeric_columns[col] = pd.to_numeric(df[col], errors="coerce")
            value_counts = numeric_series.value_counts().sort_index()
            ci_low, ci_high = mean_confidence_interval(numeric_series)
            charts.append(
                {
                    "question": question,
                    "type": "numeric",
                    "count": int(numeric_series.count()),
                    "avg": _round_or_none(numeric_series.mean()),
                    "median": _round_or_none(numeric_series.median()),
                    "min": _round_or_none(numeric_series.min()),
                    "max": _round_or_none(numeric_series.max()),
                    "std": _round_or_none(numeric_series.std()) if numeric_series.count() > 1 else 0,
                    "ci_low": ci_low,
                    "ci_high": ci_high,
                    "counts": [
                        {"value": _round_or_none(value), "total": int(total)}
                        for value, total in value_counts.items()
                    ]
                    if question.data_type == Question.DataType.DISCRETE
                    else [],
                }
            )
        elif question.data_type in {Question.DataType.NOMINAL, Question.DataType.ORDINAL}:
            text_series = series.astype(str).str.strip()
            if question.kind == Question.Kind.MULTIPLE_CHOICE or text_series.str.contains(",").any():
                frequency_series = text_series.str.split(",").explode().str.strip()
                frequency_series = frequency_series[frequency_series != ""]
            else:
                frequency_series = text_series[text_series != ""]

            if frequency_series.empty:
                continue

            if question.data_type == Question.DataType.NOMINAL and question.kind != Question.Kind.MULTIPLE_CHOICE:
                nominal_columns[col] = clean_category_series(col)
            elif question.data_type == Question.DataType.ORDINAL:
                encoded, _ = encode_ordinal(question, col)
                if encoded is not None:
                    ordinal_columns[col] = encoded

            counts = frequency_series.value_counts()
            count_rows = []
            total_count = int(counts.sum())
            for value, total in counts.items():
                count_rows.append(
                    {
                        "value": str(value),
                        "total": int(total),
                        "percent": _round_or_none((int(total) / total_count) * 100) if total_count else 0,
                    }
                )
            charts.append(
                {
                    "question": question,
                    "type": "category",
                    "counts": count_rows,
                }
            )

    inferential_analysis = []
    continuous_cols = [
        f"Q_{question.id}"
        for question in questions
        if question.data_type == Question.DataType.CONTINUOUS and f"Q_{question.id}" in numeric_columns
    ]
    nominal_cols = [
        f"Q_{question.id}"
        for question in questions
        if question.data_type == Question.DataType.NOMINAL
        and question.kind != Question.Kind.MULTIPLE_CHOICE
        and f"Q_{question.id}" in nominal_columns
    ]
    ordinal_cols = [
        f"Q_{question.id}"
        for question in questions
        if question.data_type == Question.DataType.ORDINAL and f"Q_{question.id}" in ordinal_columns
    ]

    # Nominal IV x continuous DV: Welch t-test for 2 groups, one-way ANOVA for 3-5 groups.
    for iv_col in nominal_cols:
        iv_series = nominal_columns[iv_col]
        for dv_col in continuous_cols:
            working = pd.DataFrame({"iv": iv_series, "dv": numeric_columns[dv_col]}).dropna()
            base_result = build_result(iv_col, dv_col, family="mean_comparison")
            group_counts = working["iv"].value_counts()
            valid_groups = group_counts[group_counts >= 2].index.tolist()

            if len(valid_groups) < 2:
                base_result["skipped_reason"] = "有效分組不足 2 組"
                inferential_analysis.append(base_result)
                continue
            if len(valid_groups) > 5:
                base_result["skipped_reason"] = "組別超過 5 組，統計雜訊過高"
                inferential_analysis.append(base_result)
                continue

            groups_data = [working.loc[working["iv"] == group, "dv"].dropna().to_numpy() for group in valid_groups]
            if not all(len(values) >= 2 for values in groups_data):
                base_result["skipped_reason"] = "部分組別內部的有效數值樣本不足"
                inferential_analysis.append(base_result)
                continue

            try:
                if len(valid_groups) == 2:
                    stat_value, p_value = stats.ttest_ind(groups_data[0], groups_data[1], equal_var=False)
                    test_name = "獨立樣本 t 檢定"
                    method_key = "welch_t_test"
                    effect_size = cohens_d(groups_data[0], groups_data[1])
                    effect_label = "Cohen's d"
                else:
                    stat_value, p_value = stats.f_oneway(*groups_data)
                    test_name = "單因子變異數分析 (ANOVA)"
                    method_key = "one_way_anova"
                    effect_size = eta_squared(groups_data)
                    effect_label = "eta squared"

                if p_value != p_value:
                    base_result["skipped_reason"] = "統計結果無法產生有效 p-value"
                else:
                    is_significant = bool(p_value < 0.05)
                    base_result.update(
                        {
                            "method_key": method_key,
                            "test_name": test_name,
                            "statistic": _round_or_none(stat_value, 4),
                            "p_value": _round_or_none(p_value, 4),
                            "effect_size": effect_size,
                            "effect_label": effect_label,
                            "is_significant": is_significant,
                            "groups": [
                                {
                                    "value": str(group),
                                    "count": int(len(values)),
                                    "avg": _round_or_none(pd.Series(values).mean()),
                                }
                                for group, values in zip(valid_groups, groups_data)
                            ],
                            "insight": (
                                f"「{question_by_col[iv_col].title}」的不同群體，"
                                f"在「{question_by_col[dv_col].title}」上"
                                f"{'有顯著' if is_significant else '沒有顯著'}差異。"
                            ),
                        }
                    )
                inferential_analysis.append(base_result)
            except Exception as exc:
                base_result["skipped_reason"] = f"運算錯誤：{exc}"
                inferential_analysis.append(base_result)

    # Nominal IV x ordinal DV: non-parametric rank tests.
    for iv_col in nominal_cols:
        iv_series = nominal_columns[iv_col]
        for dv_col in ordinal_cols:
            working = pd.DataFrame({"iv": iv_series, "dv": ordinal_columns[dv_col]}).dropna()
            base_result = build_result(iv_col, dv_col, family="nonparametric_rank")
            group_counts = working["iv"].value_counts()
            valid_groups = group_counts[group_counts >= 2].index.tolist()

            if len(valid_groups) < 2:
                base_result["skipped_reason"] = "有效分組不足 2 組"
                inferential_analysis.append(base_result)
                continue
            if len(valid_groups) > 5:
                base_result["skipped_reason"] = "組別超過 5 組，統計雜訊過高"
                inferential_analysis.append(base_result)
                continue

            groups_data = [working.loc[working["iv"] == group, "dv"].dropna().to_numpy() for group in valid_groups]
            if not all(len(values) >= 2 for values in groups_data):
                base_result["skipped_reason"] = "部分組別內部的有效排序樣本不足"
                inferential_analysis.append(base_result)
                continue

            try:
                if len(valid_groups) == 2:
                    stat_value, p_value = stats.mannwhitneyu(groups_data[0], groups_data[1], alternative="two-sided")
                    test_name = "Mann-Whitney U 檢定"
                    method_key = "mann_whitney_u"
                else:
                    stat_value, p_value = stats.kruskal(*groups_data)
                    test_name = "Kruskal-Wallis 檢定"
                    method_key = "kruskal_wallis"

                is_significant = bool(p_value < 0.05)
                base_result.update(
                    {
                        "method_key": method_key,
                        "test_name": test_name,
                        "statistic": _round_or_none(stat_value, 4),
                        "p_value": _round_or_none(p_value, 4),
                        "is_significant": is_significant,
                        "groups": [
                            {
                                "value": str(group),
                                "count": int(len(values)),
                                "avg": _round_or_none(pd.Series(values).median()),
                            }
                            for group, values in zip(valid_groups, groups_data)
                        ],
                        "insight": (
                            f"「{question_by_col[iv_col].title}」的不同群體，"
                            f"在「{question_by_col[dv_col].title}」的排序分布上"
                            f"{'有顯著' if is_significant else '沒有顯著'}差異。"
                        ),
                    }
                )
                inferential_analysis.append(base_result)
            except Exception as exc:
                base_result["skipped_reason"] = f"運算錯誤：{exc}"
                inferential_analysis.append(base_result)

    # Nominal x nominal: chi-square test of independence.
    for idx, left_col in enumerate(nominal_cols):
        for right_col in nominal_cols[idx + 1 :]:
            working = pd.DataFrame(
                {"left": nominal_columns[left_col], "right": nominal_columns[right_col]}
            ).dropna()
            base_result = build_result(left_col, right_col, family="categorical_association", method_key="chi_square")
            if working.empty:
                base_result["skipped_reason"] = "有效類別配對不足"
                inferential_analysis.append(base_result)
                continue

            contingency = pd.crosstab(working["left"], working["right"])
            if contingency.shape[0] < 2 or contingency.shape[1] < 2:
                base_result["skipped_reason"] = "列聯表至少需要 2x2 類別"
                inferential_analysis.append(base_result)
                continue
            if contingency.size > 25:
                base_result["skipped_reason"] = "類別組合超過 25 格，統計雜訊過高"
                inferential_analysis.append(base_result)
                continue

            try:
                chi2, p_value, dof, expected = stats.chi2_contingency(contingency)
                low_expected = int((expected < 5).sum())
                is_significant = bool(p_value < 0.05)
                total = int(contingency.to_numpy().sum())
                min_dimension = min(contingency.shape) - 1
                cramers_v = _round_or_none((chi2 / (total * min_dimension)) ** 0.5, 4) if total and min_dimension else None
                base_result.update(
                    {
                        "test_name": "卡方獨立性檢定",
                        "statistic": _round_or_none(chi2, 4),
                        "p_value": _round_or_none(p_value, 4),
                        "effect_size": cramers_v,
                        "effect_label": "Cramer's V",
                        "degrees_of_freedom": int(dof),
                        "is_significant": is_significant,
                        "groups": [
                            {"value": str(index), "count": int(row.sum()), "avg": None}
                            for index, row in contingency.iterrows()
                        ],
                        "warning": "部分期望次數低於 5，結果需謹慎解讀。" if low_expected else "",
                        "insight": (
                            f"「{question_by_col[left_col].title}」與「{question_by_col[right_col].title}」"
                            f"{'有顯著關聯' if is_significant else '沒有顯著關聯'}。"
                        ),
                    }
                )
                inferential_analysis.append(base_result)
            except Exception as exc:
                base_result["skipped_reason"] = f"運算錯誤：{exc}"
                inferential_analysis.append(base_result)

    # Correlation: Pearson for continuous x continuous, Spearman when ordinal participates.
    correlation_inputs = [(col, "continuous", numeric_columns[col]) for col in continuous_cols]
    correlation_inputs.extend((col, "ordinal", ordinal_columns[col]) for col in ordinal_cols)
    for idx, (left_col, left_type, left_series) in enumerate(correlation_inputs):
        for right_col, right_type, right_series in correlation_inputs[idx + 1 :]:
            working = pd.DataFrame({"left": left_series, "right": right_series}).dropna()
            method_key = "pearson" if left_type == right_type == "continuous" else "spearman"
            base_result = build_result(left_col, right_col, family="correlation", method_key=method_key)
            if len(working) < 3:
                base_result["skipped_reason"] = "相關分析至少需要 3 筆有效配對資料"
                inferential_analysis.append(base_result)
                continue

            try:
                if method_key == "pearson":
                    stat_value, p_value = stats.pearsonr(working["left"], working["right"])
                    test_name = "Pearson 相關分析"
                else:
                    stat_value, p_value = stats.spearmanr(working["left"], working["right"])
                    test_name = "Spearman 等級相關分析"

                if p_value != p_value:
                    base_result["skipped_reason"] = "統計結果無法產生有效 p-value"
                else:
                    is_significant = bool(p_value < 0.05)
                    direction = "正相關" if stat_value > 0 else "負相關" if stat_value < 0 else "無方向"
                    base_result.update(
                        {
                            "test_name": test_name,
                            "statistic": _round_or_none(stat_value, 4),
                            "p_value": _round_or_none(p_value, 4),
                            "is_significant": is_significant,
                            "insight": (
                                f"「{question_by_col[left_col].title}」與「{question_by_col[right_col].title}」"
                                f"呈現{direction}，且{'達顯著' if is_significant else '未達顯著'}。"
                            ),
                        }
                    )
                inferential_analysis.append(base_result)
            except Exception as exc:
                base_result["skipped_reason"] = f"運算錯誤：{exc}"
                inferential_analysis.append(base_result)

    return {"charts": charts, "inferential_analysis": inferential_analysis}


def get_stats_payload(slug):
    survey = Survey.objects.filter(slug=slug).first() if slug else None
    if not survey:
        return {
            "charts": [],
            "question_analysis": [],
            "inferential_analysis": [],
            "available_tests_count": 0,
            "skipped_tests_count": 0,
        }

    question_signature = tuple(
        Question.objects.filter(survey=survey)
        .order_by("order", "id")
        .values_list("id", "title", "kind", "data_type", "options_text", "order")
    )
    answer_signature = Answer.objects.filter(question__survey=survey).aggregate(count=Count("id"), max_id=Max("id"))
    cache_key = (survey.id, question_signature, answer_signature["count"], answer_signature["max_id"])
    cached_payload = _STATS_PAYLOAD_CACHE.get(cache_key)
    if cached_payload is not None:
        return cached_payload

    pandas_stats = get_survey_pandas_stats(survey)
    available_tests_count = sum(1 for item in pandas_stats["inferential_analysis"] if not item.get("skipped_reason"))
    skipped_tests_count = sum(1 for item in pandas_stats["inferential_analysis"] if item.get("skipped_reason"))
    payload = {
        "charts": pandas_stats["charts"] or chart_summary(survey),
        "question_analysis": [
            {
                "title": question.title,
                "data_type": question.get_data_type_display(),
                "analysis": recommend_analysis(question),
            }
            for question in survey.questions.all()
        ],
        "inferential_analysis": pandas_stats["inferential_analysis"],
        "available_tests_count": available_tests_count,
        "skipped_tests_count": skipped_tests_count,
    }
    if len(_STATS_PAYLOAD_CACHE) >= _STATS_PAYLOAD_CACHE_MAX_SIZE:
        _STATS_PAYLOAD_CACHE.clear()
    _STATS_PAYLOAD_CACHE[cache_key] = payload
    return payload


def get_text_analysis_payload(slug):
    survey = Survey.objects.filter(slug=slug).first() if slug else None
    if not survey:
        return {"keywords": [], "summary": {}, "category_sentiments": []}
    return {
        "keywords": keyword_summary(survey),
        "summary": text_analysis_summary(survey),
        "category_sentiments": category_sentiment_summary(survey),
    }


def submit_survey_payload(survey, *, user, respondent_name, respondent_email, consent_follow_up, answers):
    submission = FeedbackSubmission.objects.create(
        survey=survey,
        user=user,
        respondent_name=respondent_name,
        respondent_email=respondent_email,
        consent_follow_up=consent_follow_up,
    )
    for question in survey.questions.all():
        key = f"question_{question.id}"
        value = answers.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            value = ", ".join(value)
        analysis_text = None
        analysis_version = None
        if question.kind in {Question.Kind.SHORT_TEXT, Question.Kind.LONG_TEXT}:
            analysis_text = build_analysis_text(value)
            analysis_version = ANALYSIS_VERSION if analysis_text else None
        sentiment_score = estimate_sentiment_score(value) if analysis_text else None
        Answer.objects.create(
            submission=submission,
            question=question,
            value=value,
            analysis_text=analysis_text,
            sentiment_score=sentiment_score,
            analysis_version=analysis_version,
        )
    return {
        "submission_id": submission.id,
        "thank_you_email_enabled": survey.thank_you_email_enabled,
        "respondent_email": submission.respondent_email,
        "survey_title": survey.title,
    }
