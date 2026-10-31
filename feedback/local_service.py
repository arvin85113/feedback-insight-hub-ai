from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Max, Q
from django.db.models.functions import TruncDate
from django.urls import reverse
from django.utils import timezone

from .models import (
    Answer,
    FeedbackSubmission,
    ImprovementDispatch,
    ImprovementNotice,
    ImprovementUpdate,
    Question,
    Survey,
    chart_summary,
    keyword_summary,
    recommend_analysis,
    category_sentiment_summary,
    text_analysis_summary,
    tokenize_feedback,
)
from .analysis_jobs import schedule_survey_analysis, suppress_analysis_scheduling

STATISTICS_VERSION = "v1"
HOME_ANNOUNCEMENT_LIMIT = 5


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


def _notice_title_summary(dispatch):
    if dispatch.notice_id:
        return dispatch.notice.subject, dispatch.notice.body
    return dispatch.improvement.title, dispatch.improvement.summary


CUSTOMER_VISIBLE_IMPROVEMENT_STATUSES = (
    ImprovementUpdate.Status.PLANNED,
    ImprovementUpdate.Status.IN_PROGRESS,
    ImprovementUpdate.Status.COMPLETED,
)


def serialize_notice_event(dispatch):
    title, summary = _notice_title_summary(dispatch)
    improvement = dispatch.improvement
    status_label = (
        improvement.get_status_display() if improvement.status in CUSTOMER_VISIBLE_IMPROVEMENT_STATUSES else None
    )
    return {
        "id": dispatch.id,
        "title": title,
        "summary": summary,
        "sent_at": dispatch.sent_at.isoformat() if dispatch.sent_at else None,
        "sent_date": format_payload_date(timezone.localtime(dispatch.sent_at)) if dispatch.sent_at else "",
        "is_read": dispatch.is_read,
        "improvement_status_label": status_label,
    }


def _survey_ref(survey):
    category = survey.category
    return {
        "id": survey.id,
        "title": survey.title,
        "slug": survey.slug,
        "category": {"id": category.id, "name": category.name} if category else None,
    }


def _is_global_notice(dispatch):
    return dispatch.notice_id is not None and dispatch.notice.audience_type == ImprovementNotice.AudienceType.GLOBAL


def _sent_dispatches(user):
    return list(
        ImprovementDispatch.objects.filter(
            Q(recipient_user=user) | Q(submission__user=user),
            delivery_status=ImprovementDispatch.DeliveryStatus.SENT,
        )
        .select_related(
            "notice", "improvement", "improvement__survey", "improvement__survey__category"
        )
        .order_by("sent_at", "id")
    )


def get_customer_home_payload(user):
    from cloudapi.receipts import receipt_rows, receipt_states

    submissions = list(
        FeedbackSubmission.objects.filter(user=user).select_related("survey", "survey__category")
    )
    surveys = {}
    latest_submitted = {}
    submission_survey_ids = set()
    for submission in submissions:
        surveys[submission.survey_id] = submission.survey
        submission_survey_ids.add(submission.survey_id)
        current = latest_submitted.get(submission.survey_id)
        if current is None or submission.submitted_at > current:
            latest_submitted[submission.survey_id] = submission.submitted_at
    for row in receipt_rows(user):
        survey_id = row["survey"]["id"]
        submitted_at = datetime.fromisoformat(row["submitted_at"])
        current = latest_submitted.get(survey_id)
        if current is None or submitted_at > current:
            latest_submitted[survey_id] = submitted_at
        surveys.setdefault(survey_id, None)
    missing = [survey_id for survey_id, survey in surveys.items() if survey is None]
    if missing:
        for survey in Survey.objects.filter(pk__in=missing).select_related("category"):
            surveys[survey.id] = survey

    states = receipt_states(user)
    events_by_survey = {}
    announcements = []
    unread_count = 0
    for dispatch in _sent_dispatches(user):
        survey_id = dispatch.improvement.survey_id
        if _is_global_notice(dispatch):
            announcements.append(dispatch)
        elif survey_id in surveys:
            events_by_survey.setdefault(survey_id, []).append(dispatch)
        else:
            continue
        if not dispatch.is_read:
            unread_count += 1

    cards = []
    for survey_id, survey in surveys.items():
        events = events_by_survey.get(survey_id, [])
        if events:
            state = "notified"
        elif survey_id not in submission_survey_ids and states.get(survey_id) == "quarantined":
            state = "processing"
        else:
            state = "waiting"
        submitted_at = latest_submitted[survey_id]
        last_sent = max((e.sent_at for e in events if e.sent_at), default=None)
        activity = max(submitted_at, last_sent) if last_sent else submitted_at
        cards.append(
            (
                activity,
                {
                    "survey": _survey_ref(survey),
                    "submitted_date": format_payload_date(timezone.localtime(submitted_at)),
                    "state": state,
                    "events": [serialize_notice_event(e) for e in events],
                },
            )
        )
    cards.sort(key=lambda item: item[0], reverse=True)
    return {
        "survey_cards": [card for _, card in cards],
        "announcements": [serialize_notice_event(d) for d in reversed(announcements)][:HOME_ANNOUNCEMENT_LIMIT],
        "unread_count": unread_count,
        "notification_opt_in": user.notification_opt_in,
    }


def get_customer_notifications_payload(user):
    by_survey = {}
    announcements = []
    unread_count = 0
    shown = 0
    for dispatch in _sent_dispatches(user):
        if _is_global_notice(dispatch):
            announcements.append(dispatch)
        elif dispatch.improvement.survey_id:
            by_survey.setdefault(dispatch.improvement.survey_id, []).append(dispatch)
        else:
            announcements.append(dispatch)
        shown += 1
        if not dispatch.is_read:
            unread_count += 1

    def build(survey, items):
        ordered = sorted(items, key=lambda d: (d.is_read, -(d.sent_at.timestamp() if d.sent_at else 0), -d.id))
        newest = max((d.sent_at for d in items if d.sent_at), default=None)
        return {
            "survey": _survey_ref(survey) if survey else None,
            "notices": [serialize_notice_event(d) for d in ordered],
            "_unread": any(not d.is_read for d in items),
            "_newest": newest.timestamp() if newest else 0,
        }

    groups = [build(items[0].improvement.survey, items) for items in by_survey.values()]
    if announcements:
        groups.append(build(None, announcements))
    groups.sort(key=lambda g: (not g["_unread"], -g["_newest"]))
    for group in groups:
        del group["_unread"], group["_newest"]
    return {
        "groups": groups,
        "notice_count": shown,
        "unread_count": unread_count,
    }


DASHBOARD_RANGES = (7, 30)
DASHBOARD_SURVEY_LIMIT = 8
DASHBOARD_ACTIVITY_LIMIT = 8
WEEKDAY_LABELS = "一二三四五六日"


def _percent_change(current, previous):
    if not previous:
        return None
    return round((current - previous) * 100 / previous)


def _survey_status(survey):
    if not survey.is_active:
        return "inactive", "已停用"
    if survey.published_version is None:
        return "draft", "草稿"
    return "live", "收件中"


def get_dashboard_payload(range_days=7):
    """Overview page: what needs attention, headline numbers, the response trend, surveys and recent activity."""
    from .models import SurveyAnalysisState

    if range_days not in DASHBOARD_RANGES:
        range_days = DASHBOARD_RANGES[0]
    today = timezone.localdate()
    range_start = today - timedelta(days=range_days - 1)
    previous_start = range_start - timedelta(days=range_days)

    from cloudapi.models import SubmissionReceipt

    valid = FeedbackSubmission.objects.filter(is_complete=True, voided_at__isnull=True)
    # Cloud: answers to node-owned surveys stay on the node; the cloud only keeps a receipt per response.
    # A reply is counted once by its uuid, as in cloudapi.receipts (spec §1 數量定義).
    receipts = SubmissionReceipt.objects.exclude(status=SubmissionReceipt.Status.ABANDONED).exclude(
        submission_uuid__in=FeedbackSubmission.objects.values("idempotency_key")
    )
    counts_by_day = {}
    for source in (valid, receipts):
        for row in (
            source.filter(submitted_at__date__gte=previous_start)
            .annotate(day=TruncDate("submitted_at"))
            .values("day")
            .annotate(total=Count("id"))
        ):
            counts_by_day[row["day"]] = counts_by_day.get(row["day"], 0) + row["total"]
    trend = []
    for offset in range(range_days):
        day = range_start + timedelta(days=offset)
        trend.append({
            "label": f"{day.month}/{day.day}",
            "total": counts_by_day.get(day, 0),
            "is_today": day == today,
            "show_label": range_days <= 7 or offset % 5 == 0 or day == today,
        })
    peak = max(point["total"] for point in trend)
    for point in trend:
        point["percent"] = round(point["total"] * 100 / peak) if peak else 0
    range_total = sum(point["total"] for point in trend)
    previous_total = sum(
        counts_by_day.get(previous_start + timedelta(days=offset), 0) for offset in range(range_days)
    )

    valid_filter = Q(submissions__is_complete=True, submissions__voided_at__isnull=True)
    surveys = list(
        Survey.objects.filter(archived_at__isnull=True)
        .annotate(
            response_total=Count("submissions", filter=valid_filter),
            recent_total=Count("submissions", filter=valid_filter & Q(submissions__submitted_at__date__gte=range_start)),
            last_response_at=Max("submissions__submitted_at", filter=valid_filter),
        )
    )
    receipt_rows = {
        row["survey"]: row
        for row in receipts.values("survey").annotate(
            total=Count("id"),
            recent=Count("id", filter=Q(submitted_at__date__gte=range_start)),
            last=Max("submitted_at"),
        )
    }
    for survey in surveys:
        row = receipt_rows.get(survey.pk)
        if row:
            survey.response_total += row["total"]
            survey.recent_total += row["recent"]
            survey.last_response_at = max(filter(None, (survey.last_response_at, row["last"])))
    surveys.sort(key=lambda survey: survey.title)
    surveys.sort(key=lambda survey: survey.last_response_at or datetime.min.replace(tzinfo=dt_timezone.utc), reverse=True)
    surveys.sort(key=lambda survey: not survey.is_active)
    states = {
        state.survey_id: state
        for state in SurveyAnalysisState.objects.filter(survey__in=[survey.pk for survey in surveys])
    }
    survey_rows = []
    attention = []
    for survey in surveys:
        status, status_label = _survey_status(survey)
        state = states.get(survey.pk)
        analyzed_at = state.published_at if state else None
        unanalyzed = 0
        # In the cloud, node-owned surveys are analysed on the node, which the cloud cannot see into.
        node_analysed = survey.owner_node_id and not settings.IS_NODE
        if survey.analysis_enabled and survey.response_total and not node_analysed:
            unanalyzed = (
                valid.filter(survey=survey, submitted_at__gt=analyzed_at).count()
                if analyzed_at
                else survey.response_total
            )
        survey_rows.append({
            "title": survey.title,
            "slug": survey.slug,
            "status": status,
            "status_label": status_label,
            "response_total": survey.response_total,
            "recent_total": survey.recent_total,
            "last_response_at": survey.last_response_at,
            "analyzed_at": analyzed_at,
            "unanalyzed": unanalyzed,
            "analysis_enabled": survey.analysis_enabled,
        })
        if status == "live" and not survey.response_total:
            attention.append({
                "kind": "share",
                "title": f"「{survey.title}」還沒有回覆",
                "detail": "問卷已發布。把 QR code 放到門市或收據上，顧客才看得到。",
                "action": "取得 QR code",
                "url": reverse("feedback:survey-qrcode", args=[survey.slug]),
            })
        elif unanalyzed:
            attention.append({
                "kind": "analysis",
                "title": f"「{survey.title}」有 {unanalyzed} 份回覆還沒分析",
                "detail": "本機工作台下次分析後，結果才會更新。" if analyzed_at else "這份問卷還沒有分析結果。",
                "action": "查看分析狀態",
                "url": reverse("feedback:analysis-operations"),
            })

    improvements = ImprovementUpdate.objects.select_related("survey").filter(archived_at__isnull=True)
    unnotified = improvements.filter(status=ImprovementUpdate.Status.COMPLETED, emailed_at__isnull=True)
    attention = [
        {
            "kind": "notify",
            "title": f"改善「{item.title}」已完成，還沒通知顧客",
            "detail": "通知提出意見的顧客，讓他們知道店家做了什麼。",
            "action": "寄出通知",
            "url": reverse("feedback:improvement-detail", args=[item.pk]),
        }
        for item in unnotified.order_by("-updated_at")[:5]
    ] + attention

    open_statuses = (ImprovementUpdate.Status.PLANNED, ImprovementUpdate.Status.IN_PROGRESS)
    notified_total = ImprovementDispatch.objects.filter(
        delivery_status=ImprovementDispatch.DeliveryStatus.SENT, sent_at__date__gte=range_start
    ).count()

    # Responses are grouped per survey and day so a busy day does not crowd out everything else.
    response_groups = {}
    for source in (valid, receipts):
        for row in (
            source.annotate(day=TruncDate("submitted_at"))
            .values("day", "survey__title")
            .annotate(total=Count("id"), last=Max("submitted_at"))
            .order_by("-last")[:DASHBOARD_ACTIVITY_LIMIT]
        ):
            group = response_groups.setdefault((row["day"], row["survey__title"]), {"total": 0, "last": row["last"]})
            group["total"] += row["total"]
            group["last"] = max(group["last"], row["last"])
    activity = [
        {"kind": "response", "at": group["last"], "title": f"「{title}」收到 {group['total']} 份回覆", "url": ""}
        for (_day, title), group in response_groups.items()
    ]
    activity += [
        {
            "kind": "improvement",
            "at": item.updated_at,
            "title": f"改善「{item.title}」：{item.get_status_display()}",
            "url": reverse("feedback:improvement-detail", args=[item.pk]),
        }
        for item in improvements.order_by("-updated_at")[:DASHBOARD_ACTIVITY_LIMIT]
    ]
    activity += [
        {
            "kind": "notice",
            "at": notice.created_at,
            "title": f"建立通知「{notice.subject}」",
            "url": reverse("feedback:notice-detail", args=[notice.pk]),
        }
        for notice in ImprovementNotice.objects.order_by("-created_at")[:DASHBOARD_ACTIVITY_LIMIT]
    ]
    activity.sort(key=lambda entry: entry["at"], reverse=True)

    return {
        "today_label": f"{today.month} 月 {today.day} 日（{WEEKDAY_LABELS[today.weekday()]}）",
        "range_days": range_days,
        "range_options": DASHBOARD_RANGES,
        "attention_items": attention,
        "kpis": [
            {
                "label": f"近 {range_days} 天回覆",
                "value": range_total,
                "change": _percent_change(range_total, previous_total),
                "hint": f"前 {range_days} 天 {previous_total} 份",
            },
            {
                "label": "收件中的問卷",
                "value": sum(1 for row in survey_rows if row["status"] == "live"),
                "hint": f"共 {len(survey_rows)} 份問卷",
            },
            {
                "label": "進行中的改善",
                "value": improvements.filter(status__in=open_statuses).count(),
                "hint": f"已完成 {improvements.filter(status=ImprovementUpdate.Status.COMPLETED).count()} 項",
            },
            {
                "label": f"近 {range_days} 天通知顧客",
                "value": notified_total,
                "hint": "已寄出的改善通知",
            },
        ],
        "trend": trend,
        "trend_peak": peak,
        "trend_total": range_total,
        "trend_average": round(range_total / range_days, 1),
        "trend_busiest": max(trend, key=lambda point: point["total"]) if range_total else None,
        "survey_rows": survey_rows[:DASHBOARD_SURVEY_LIMIT],
        "survey_total": len(survey_rows),
        "activity": activity[:DASHBOARD_ACTIVITY_LIMIT],
    }


def _round_or_none(value, digits=2):
    if value != value:
        return None
    return round(float(value), digits)


def _round_p_value(value):
    return _round_or_none(value, 6)


def get_survey_pandas_stats(survey):
    import pandas as pd

    from .analysis_adapters import answer_cell

    questions = list(survey.questions.filter(is_active=True).order_by("order", "id"))
    by_id = {question.pk: question for question in questions}
    submission_ids = FeedbackSubmission.objects.filter(
        survey=survey, is_complete=True, voided_at__isnull=True
    ).order_by("pk").values_list("pk", flat=True)
    # Every complete reply is a row, even one with no answers, like the Worker input (spec §7.3).
    records = {pk: {f"Q_{question.pk}": None for question in questions} for pk in submission_ids}
    answer_rows = Answer.objects.filter(
        submission_id__in=list(records), question_id__in=list(by_id)
    ).values_list("submission_id", "question_id", "value", "choice_codes")
    for submission_id, question_id, value, codes in answer_rows.iterator(chunk_size=2000):
        records[submission_id][f"Q_{question_id}"] = answer_cell(by_id[question_id], value, codes)
    return analyze_frame(questions, pd.DataFrame(list(records.values())))


def _is_missing(value):
    if value is None:
        return True
    if isinstance(value, (list, tuple)):
        return len(value) == 0
    try:
        import pandas as pd

        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip() == ""


def classify_values(series, *, data_type, kind, levels, excluded):
    """Split one column into valid / missing / excluded / invalid, never overlapping (spec §2.2)."""

    import pandas as pd

    levels = [str(level) for level in levels]
    excluded = [str(item) for item in excluded]
    missing = series.map(_is_missing).astype(bool)
    if kind == Question.Kind.MULTIPLE_CHOICE:
        level_set = set(levels)

        def multi_valid(value):
            items = value if isinstance(value, (list, tuple)) else [value]
            return not level_set or all(str(item) in level_set for item in items)

        valid = ~missing & series.map(lambda value: False if _is_missing(value) else multi_valid(value)).astype(bool)
        excluded_mask = pd.Series(False, index=series.index)
    elif data_type in {Question.DataType.CONTINUOUS, Question.DataType.DISCRETE}:
        valid = ~missing & pd.to_numeric(series.where(~missing), errors="coerce").notna()
        excluded_mask = pd.Series(False, index=series.index)
    elif data_type in {Question.DataType.NOMINAL, Question.DataType.ORDINAL}:
        text = series.map(lambda value: None if _is_missing(value) else str(value).strip())
        valid = ~missing & (text.isin(levels) if levels else ~missing)
        excluded_mask = ~missing & text.isin(excluded)
    else:
        valid = ~missing
        excluded_mask = pd.Series(False, index=series.index)
    invalid = ~missing & ~valid & ~excluded_mask
    return {
        "total_n": int(len(series)),
        "valid_n": int(valid.sum()),
        "missing_n": int(missing.sum()),
        "excluded_n": int(excluded_mask.sum()),
        "invalid_n": int(invalid.sum()),
        "valid_mask": valid,
        "excluded_mask": excluded_mask,
    }


def analyze_frame(questions, df):
    """Shared calculation boundary: question descriptors + aligned rows; no ORM I/O."""
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

    if not questions or df.empty:
        return {"charts": [], "inferential_analysis": []}
    df = df.copy()
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
        return df[col].astype("string").str.strip().replace("", pd.NA)

    def encode_ordinal(question, col):
        ordered_options = question.analysis_options
        if not ordered_options:
            return None, "順序題缺少選項清單，無法安全轉成排序分數"
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

    def format_numeric_bucket(value):
        rounded = _round_or_none(value)
        if rounded is None:
            return ""
        if float(rounded).is_integer():
            return str(int(rounded))
        return str(rounded)

    def numeric_distribution(question, numeric_series):
        """Keep review-length charts readable without changing the raw feature."""

        code = str(getattr(question, "code", "") or "").lower()
        title = str(getattr(question, "title", "") or "").lower()
        is_review_length = (
            code == "review_length"
            or "review length" in title
            or "評論長度" in title
        )
        if not is_review_length:
            return numeric_series.value_counts().sort_index(), None

        bins = [-float("inf"), 99, 249, 499, 999, 1999, float("inf")]
        labels = ["少於 100", "100–249", "250–499", "500–999", "1,000–1,999", "2,000 以上"]
        grouped = pd.cut(numeric_series, bins=bins, labels=labels, include_lowest=True)
        counts = grouped.value_counts(sort=False)
        return counts[counts > 0], "字元"

    for question in questions:
        col = f"Q_{question.id}"
        if col not in df.columns:
            continue

        coverage = classify_values(
            df[col], data_type=question.data_type, kind=question.kind,
            levels=question.analysis_options, excluded=question.analysis_excluded_options,
        )
        counts_fields = {name: coverage[name] for name in ("valid_n", "missing_n", "excluded_n", "invalid_n")}
        raw = df[col]
        # Excluded and invalid values never reach a test (spec §2.2).
        df[col] = raw.where(coverage["valid_mask"], None)
        series = df[col].dropna()
        if series.empty:
            continue

        if question.data_type in {Question.DataType.CONTINUOUS, Question.DataType.DISCRETE}:
            numeric_series = pd.to_numeric(series, errors="coerce").dropna()
            if numeric_series.empty:
                continue
            numeric_columns[col] = pd.to_numeric(df[col], errors="coerce")
            value_counts, distribution_unit = numeric_distribution(question, numeric_series)
            total_count = int(value_counts.sum())
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
                    "q1": _round_or_none(numeric_series.quantile(0.25)),
                    "q3": _round_or_none(numeric_series.quantile(0.75)),
                    "p95": _round_or_none(numeric_series.quantile(0.95)),
                    "distribution_unit": distribution_unit,
                    "counts": [
                        {
                            "value": str(value) if distribution_unit else format_numeric_bucket(value),
                            "total": int(total),
                            "percent": _round_or_none((int(total) / total_count) * 100) if total_count else 0,
                        }
                        for value, total in value_counts.items()
                    ],
                    **counts_fields,
                }
            )
        elif question.kind == Question.Kind.MULTIPLE_CHOICE:
            # Multiple response: who selected each option vs. its share of all checks (spec §2.3).
            answered_n = int(series.count())
            exploded = series.map(lambda value: list(value) if isinstance(value, (list, tuple)) else [value]).explode()
            exploded = exploded.astype(str).str.strip()
            counts = exploded.value_counts()
            checks = int(counts.sum())
            charts.append(
                {
                    "question": question,
                    "type": "category",
                    "multi": True,
                    "answered_n": answered_n,
                    "counts": [
                        {
                            "value": str(value),
                            "total": int(total),
                            "selection_rate": _round_or_none(int(total) / answered_n * 100) if answered_n else 0,
                            "check_share": _round_or_none(int(total) / checks * 100) if checks else 0,
                        }
                        for value, total in counts.items()
                    ],
                    **counts_fields,
                }
            )
        elif question.data_type in {Question.DataType.NOMINAL, Question.DataType.ORDINAL}:
            frequency_series = series.astype(str).str.strip()
            frequency_series = frequency_series[frequency_series != ""]
            if frequency_series.empty:
                continue

            if question.data_type == Question.DataType.NOMINAL:
                nominal_columns[col] = clean_category_series(col)
            else:
                encoded, _ = encode_ordinal(question, col)
                if encoded is not None:
                    ordinal_columns[col] = encoded

            counts = frequency_series.value_counts()
            total_count = int(counts.sum())
            excluded_values = raw[coverage["excluded_mask"]].astype(str).str.strip().value_counts()
            charts.append(
                {
                    "question": question,
                    "type": "category",
                    "counts": [
                        {
                            "value": str(value),
                            "total": int(total),
                            "percent": _round_or_none((int(total) / total_count) * 100) if total_count else 0,
                        }
                        for value, total in counts.items()
                    ],
                    "excluded_counts": [
                        {"value": str(value), "total": int(total)} for value, total in excluded_values.items()
                    ],
                    **counts_fields,
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
                            "p_value": _round_p_value(p_value),
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
                        "p_value": _round_p_value(p_value),
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
                        "p_value": _round_p_value(p_value),
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
            base_result["valid_n"] = len(working)
            base_result["excluded_n"] = len(df) - len(working)
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
                            "p_value": _round_p_value(p_value),
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


def build_stats_payload(survey):
    pandas_stats = get_survey_pandas_stats(survey)
    available_tests_count = sum(1 for item in pandas_stats["inferential_analysis"] if not item.get("skipped_reason"))
    skipped_tests_count = sum(1 for item in pandas_stats["inferential_analysis"] if item.get("skipped_reason"))
    return {
        "charts": pandas_stats["charts"] or chart_summary(survey),
        "question_analysis": [
            {
                "title": question.title,
                "data_type": question.get_data_type_display(),
                "analysis": recommend_analysis(question),
            }
            for question in survey.questions.filter(is_active=True)
        ],
        "inferential_analysis": pandas_stats["inferential_analysis"],
        "available_tests_count": available_tests_count,
        "skipped_tests_count": skipped_tests_count,
    }


def build_text_analysis_payload(survey):
    keywords = keyword_summary(survey)
    response_counts = {}
    tracked_keywords = {row["keyword"] for row in keywords}
    if tracked_keywords:
        for analysis_text, value in Answer.objects.filter(
            question__survey=survey,
            question__is_active=True,
            question__enable_keyword_tracking=True,
            submission__is_complete=True,
            submission__voided_at__isnull=True,
        ).values_list("analysis_text", "value"):
            for keyword in set(tokenize_feedback(analysis_text or value)) & tracked_keywords:
                response_counts[keyword] = response_counts.get(keyword, 0) + 1
        keywords = [
            {**row, "response_count": response_counts.get(row["keyword"], 0)}
            for row in keywords
        ]
    return {
        "keywords": keywords,
        "summary": text_analysis_summary(survey),
        "category_sentiments": category_sentiment_summary(survey),
    }


def _submission_result(submission, *, reused=False):
    return {
        "submission_id": submission.id,
        "thank_you_email_enabled": submission.survey.thank_you_email_enabled,
        "respondent_email": submission.respondent_email,
        "survey_title": submission.survey.title,
        "reused": reused,
    }


class SurveyNotAccepting(Exception):
    user_message = "這份問卷目前未開放填答。"


class SurveyFormOutdated(Exception):
    user_message = "問卷已更新，請確認後重新送出"


class SurveyAlreadySubmitted(Exception):
    user_message = "你已填答過這份問卷。"


def _encode_answer(question, value):
    """(choice_codes, display value) for one answer; choice answers accept codes or exact labels."""

    if question.kind not in (Question.Kind.SINGLE_CHOICE, Question.Kind.MULTIPLE_CHOICE):
        return None, str(value)
    by_code = {choice["code"]: choice["label"] for choice in question.choices}
    by_label = {choice["label"]: choice["code"] for choice in question.choices}
    items = value if isinstance(value, (list, tuple)) else [value]
    codes = []
    for item in items:
        item = str(item)
        code = item if item in by_code else by_label.get(item.strip())
        if code is None:
            raise ValueError("選項不在題目中")
        codes.append(code)
    order = [choice["code"] for choice in question.choices]
    codes = sorted(set(codes), key=order.index)
    return codes, ", ".join(by_code[code] for code in codes)


def _encoded_answers(survey, answers):
    encoded = {}
    for question in survey.questions.filter(is_active=True):
        value = answers.get(f"question_{question.id}")
        if value is None or value == "" or value == []:
            continue
        encoded[question.id] = _encode_answer(question, value)
    return encoded


def _same_submission(submission, survey, user, form_version, consent_follow_up, encoded):
    stored = {
        answer.question_id: (answer.choice_codes, answer.value)
        for answer in submission.answers.all()
    }
    return (
        submission.survey_id == survey.pk
        and submission.user_id == getattr(user, "pk", None)
        and submission.definition_version == form_version
        and submission.consent_follow_up == consent_follow_up
        and stored == encoded
    )


def submit_survey_payload(
    survey,
    *,
    user,
    respondent_name,
    respondent_email,
    consent_follow_up,
    answers,
    idempotency_key=None,
    form_version=None,
    reject_repeat=False,
):
    """Write one reply (builder spec §7.1, §7.3).

    Under the survey row lock and in this order: a resend of the same reply returns the earlier
    result; a new reply needs a published survey that accepts replies and the published version
    the form was built from.  `form_version=None` is for trusted internal callers (seeds,
    imports) and means the current published version.
    """

    with transaction.atomic():
        survey = Survey.objects.select_for_update().get(pk=survey.pk)
        version = survey.published_version if form_version is None else form_version
        encoded = _encoded_answers(survey, answers)
        if idempotency_key:
            existing = FeedbackSubmission.objects.filter(idempotency_key=idempotency_key).first()
            if existing is not None:
                if not _same_submission(existing, survey, user, version, consent_follow_up, encoded):
                    raise ValueError("idempotency key 已由其他填答使用")
                return _submission_result(existing, reused=True)
        if not survey.accepts_responses:
            raise SurveyNotAccepting()
        if version != survey.published_version:
            raise SurveyFormOutdated()
        if reject_repeat and user is not None:
            from cloudapi.receipts import has_submitted

            if has_submitted(survey, user):
                raise SurveyAlreadySubmitted()
        with suppress_analysis_scheduling():
            submission = FeedbackSubmission.objects.create(
                survey=survey,
                user=user,
                respondent_name=respondent_name,
                respondent_email=respondent_email,
                consent_follow_up=consent_follow_up,
                definition_version=survey.published_version,
                **({"idempotency_key": idempotency_key} if idempotency_key else {}),
            )
            Answer.objects.bulk_create(
                Answer(submission=submission, question_id=question_id, value=value, choice_codes=codes)
                for question_id, (codes, value) in encoded.items()
            )
        schedule_survey_analysis(survey.pk, change="input")
    return _submission_result(submission)
