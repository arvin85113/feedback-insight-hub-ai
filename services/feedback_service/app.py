from collections import defaultdict
from datetime import datetime, timedelta

from flask import Flask, jsonify, request
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from feedback.text_pipeline import ANALYSIS_VERSION, build_analysis_text, estimate_sentiment_score, tokenize_feedback

from .analysis import DATA_TYPE_LABELS, build_dashboard_insights, summarize_keywords, summarize_numeric
from .db import session_scope
from .models import Answer, FeedbackSubmission, ImprovementDispatch, ImprovementUpdate, KeywordCategory, Question, Survey, User


app = Flask(__name__)


def build_category_sentiments(answer_rows, category_map):
    bucket = {}
    for row in answer_rows:
        tokens = tokenize_feedback(row.analysis_text or row.value or "")
        if not tokens:
            continue
        categories = {category_map.get(token, "未分類") for token in tokens}
        for category in categories:
            entry = bucket.setdefault(
                category,
                {"category": category, "positive": 0, "neutral": 0, "negative": 0, "total": 0},
            )
            entry["total"] += 1
            if row.sentiment_score is None:
                entry["neutral"] += 1
            elif row.sentiment_score > 0.1:
                entry["positive"] += 1
            elif row.sentiment_score < -0.1:
                entry["negative"] += 1
            else:
                entry["neutral"] += 1
    return sorted(bucket.values(), key=lambda item: item["total"], reverse=True)


def format_payload_date(value: datetime) -> str:
    return f"{value.year}/{value.month}/{value.day}"


def format_payload_datetime(value: datetime) -> str:
    return f"{format_payload_date(value)} {value:%H:%M}"


def serialize_survey(survey: Survey, response_total: int | None = None) -> dict:
    return {
        "id": survey.id,
        "title": survey.title,
        "slug": survey.slug,
        "description": survey.description,
        "improvement_tracking_enabled": survey.improvement_tracking_enabled,
        "thank_you_email_enabled": survey.thank_you_email_enabled,
        "questions": {"count": len(survey.questions)},
        "submissions": {"count": response_total if response_total is not None else len(survey.submissions)},
    }


def serialize_submission(submission: FeedbackSubmission, answer_count: int | None = None) -> dict:
    display_name = submission.respondent_name
    if not display_name and submission.user:
        display_name = f"{submission.user.first_name} {submission.user.last_name}".strip() or submission.user.username
    category = submission.survey.category
    return {
        "id": submission.id,
        "submitted_at": submission.submitted_at.isoformat(),
        "submitted_date": format_payload_date(submission.submitted_at),
        "submitted_datetime": format_payload_datetime(submission.submitted_at),
        "consent_follow_up": submission.consent_follow_up,
        "respondent_email": submission.respondent_email,
        "display_name": display_name or "匿名填答者",
        "survey": {
            "id": submission.survey.id,
            "title": submission.survey.title,
            "slug": submission.survey.slug,
            "category": {"id": category.id, "name": category.name} if category else None,
        },
        "answers": {"count": answer_count if answer_count is not None else len(submission.answers)},
    }


def serialize_notice(dispatch: ImprovementDispatch) -> dict:
    return {
        "id": dispatch.id,
        "sent_at": dispatch.sent_at.isoformat(),
        "personalized_note": dispatch.personalized_note,
        "submission": {
            "id": dispatch.submission.id,
            "survey": {
                "id": dispatch.submission.survey.id,
                "title": dispatch.submission.survey.title,
                "slug": dispatch.submission.survey.slug,
            },
        },
        "improvement": {
            "id": dispatch.improvement.id,
            "title": dispatch.improvement.title,
            "summary": dispatch.improvement.summary,
        },
    }


def build_submission_preview(submission: FeedbackSubmission) -> str:
    answers = sorted(submission.answers, key=lambda answer: (answer.question.order, answer.question.id))
    for answer in answers:
        value = (answer.value or "").strip()
        if not value:
            continue
        if len(value) > 42:
            value = f"{value[:42]}..."
        return f"{answer.question.title}：{value}"
    return ""


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/api/home")
def home():
    with session_scope() as session:
        surveys = session.scalars(
            select(Survey)
            .options(joinedload(Survey.questions))
            .where(Survey.is_active.is_(True))
            .order_by(Survey.title)
        ).unique().all()
        total_submissions = session.scalar(select(func.count(FeedbackSubmission.id))) or 0
        total_improvements = session.scalar(select(func.count(ImprovementUpdate.id))) or 0
        response_counts = dict(
            session.execute(
                select(FeedbackSubmission.survey_id, func.count(FeedbackSubmission.id)).group_by(FeedbackSubmission.survey_id)
            ).all()
        )
        active_survey_count = len(surveys)
        return jsonify(
            {
                "active_survey_count": active_survey_count,
                "response_count": total_submissions,
                "improvement_count": total_improvements,
                # TODO: Deprecate these marketing-style homepage fields alongside the Django fallback payload.
                "managed_clients": max(12, active_survey_count * 3 or 12),
                "response_velocity": round(total_submissions / active_survey_count, 1) if active_survey_count else 0,
                "surveys": [serialize_survey(survey, response_counts.get(survey.id, 0)) for survey in surveys[:6]],
            }
        )


@app.get("/api/customers/<int:user_id>/home")
def customer_home(user_id: int):
    with session_scope() as session:
        user = session.get(User, user_id)
        if not user:
            return jsonify({"message": "User not found"}), 404

        submissions = session.scalars(
            select(FeedbackSubmission)
            .options(
                joinedload(FeedbackSubmission.survey).joinedload(Survey.category),
                joinedload(FeedbackSubmission.user),
                joinedload(FeedbackSubmission.answers).joinedload(Answer.question),
            )
            .where(FeedbackSubmission.user_id == user_id)
            .order_by(FeedbackSubmission.submitted_at.desc())
        ).unique().all()
        notices = session.scalars(
            select(ImprovementDispatch)
            .options(
                joinedload(ImprovementDispatch.improvement),
                joinedload(ImprovementDispatch.submission).joinedload(FeedbackSubmission.survey),
            )
            .join(FeedbackSubmission, ImprovementDispatch.submission_id == FeedbackSubmission.id)
            .where(FeedbackSubmission.user_id == user_id)
            .order_by(ImprovementDispatch.sent_at.desc())
        ).unique().all()

        submission_rows = []
        for submission in submissions[:8]:
            latest_notice = next((notice for notice in notices if notice.submission_id == submission.id), None)
            submission_rows.append(
                {
                    "submission": serialize_submission(submission, len(submission.answers)),
                    "answer_count": len(submission.answers),
                    "answer_preview": build_submission_preview(submission),
                    "latest_notice": serialize_notice(latest_notice) if latest_notice else None,
                }
            )

        return jsonify(
            {
                "submissions": [serialize_submission(item, len(item.answers)) for item in submissions],
                "notices": [serialize_notice(item) for item in notices[:10]],
                "submission_rows": submission_rows,
                "submission_count": len(submissions),
                "active_follow_up_count": sum(1 for item in submissions if item.consent_follow_up),
                "latest_submission": serialize_submission(submissions[0], len(submissions[0].answers)) if submissions else None,
                "latest_notice": serialize_notice(notices[0]) if notices else None,
                "subscribed_survey_count": len({item.survey_id for item in submissions}),
            }
        )


@app.get("/api/customers/<int:user_id>/notifications")
def customer_notifications(user_id: int):
    with session_scope() as session:
        notices = session.scalars(
            select(ImprovementDispatch)
            .options(
                joinedload(ImprovementDispatch.improvement),
                joinedload(ImprovementDispatch.submission).joinedload(FeedbackSubmission.survey),
            )
            .join(FeedbackSubmission, ImprovementDispatch.submission_id == FeedbackSubmission.id)
            .where(FeedbackSubmission.user_id == user_id)
            .order_by(ImprovementDispatch.sent_at.desc())
        ).unique().all()
        return jsonify(
            {
                "notices": [serialize_notice(item) for item in notices],
                "notice_count": len(notices),
                "latest_notice": serialize_notice(notices[0]) if notices else None,
            }
        )


@app.get("/api/dashboard")
def dashboard():
    with session_scope() as session:
        start = datetime.now().date() - timedelta(days=6)

        daily_qs = session.execute(
            select(
                func.date(FeedbackSubmission.submitted_at).label("day"),
                func.count(FeedbackSubmission.id).label("total"),
            )
            .where(FeedbackSubmission.submitted_at >= start)
            .group_by(func.date(FeedbackSubmission.submitted_at))
        ).all()
        counts_by_day = {str(row.day): row.total for row in daily_qs}
        daily_counts = [
            {
                "label": (start + timedelta(days=offset)).strftime("%m/%d"),
                "total": counts_by_day.get(str(start + timedelta(days=offset)), 0),
            }
            for offset in range(7)
        ]

        response_counts = dict(
            session.execute(
                select(FeedbackSubmission.survey_id, func.count(FeedbackSubmission.id))
                .group_by(FeedbackSubmission.survey_id)
            ).all()
        )
        surveys = session.scalars(
            select(Survey).options(joinedload(Survey.questions)).order_by(Survey.title)
        ).unique().all()
        recent_submissions = session.scalars(
            select(FeedbackSubmission)
            .options(joinedload(FeedbackSubmission.survey), joinedload(FeedbackSubmission.user))
            .order_by(FeedbackSubmission.submitted_at.desc())
            .limit(6)
        ).unique().all()
        improvements = session.scalars(
            select(ImprovementUpdate)
            .options(joinedload(ImprovementUpdate.survey))
            .order_by(ImprovementUpdate.created_at.desc())
        ).unique().all()

        top_surveys = sorted(surveys, key=lambda item: response_counts.get(item.id, 0), reverse=True)[:5]
        total_surveys = len(surveys)
        total_submissions = session.scalar(select(func.count(FeedbackSubmission.id))) or 0
        total_improvements = len(improvements)
        active_surveys = sum(1 for item in surveys if item.is_active)
        emailed_improvements = sum(1 for item in improvements if item.emailed_at)
        insights = build_dashboard_insights(
            total_surveys=total_surveys,
            total_submissions=total_submissions,
            total_improvements=total_improvements,
            top_survey_title=top_surveys[0].title if top_surveys else None,
        )

        return jsonify(
            {
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
                        "value": round(total_submissions / total_surveys, 1) if total_surveys else 0,
                        "hint": "每份問卷平均回覆數",
                        "accent": "amber",
                    },
                ],
                "daily_counts": daily_counts,
                "top_surveys": [serialize_survey(item, response_counts.get(item.id, 0)) for item in top_surveys],
                "recent_responses": [serialize_submission(item) for item in recent_submissions],
                "latest_improvements": [
                    {
                        "id": item.id,
                        "title": item.title,
                        "summary": item.summary,
                        "emailed_at": item.emailed_at.isoformat() if item.emailed_at else None,
                        "survey": {"id": item.survey.id, "title": item.survey.title, "slug": item.survey.slug},
                    }
                    for item in improvements[:5]
                ],
                "insights": insights,
                "action_items": [
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
                ],
            }
        )


@app.get("/api/stats")
def stats():
    slug = request.args.get("survey")
    with session_scope() as session:
        survey = session.scalar(select(Survey).where(Survey.slug == slug)) if slug else None
        if not survey:
            return jsonify({"charts": [], "question_analysis": []})
        questions = session.scalars(select(Question).where(Question.survey_id == survey.id).order_by(Question.order)).all()
        all_answers = session.scalars(
            select(Answer).where(Answer.question_id.in_([q.id for q in questions]))
        ).all()
        answers_by_qid = defaultdict(list)
        for a in all_answers:
            answers_by_qid[a.question_id].append(a)

        charts = []
        question_analysis = []
        for question in questions:
            answers = answers_by_qid[question.id]
            if question.kind in {"integer", "decimal", "scale"}:
                numeric_values = []
                for answer in answers:
                    try:
                        numeric_values.append(float(answer.value))
                    except (TypeError, ValueError):
                        continue
                summary = summarize_numeric(numeric_values)
                if summary:
                    charts.append({"question": {"title": question.title}, "type": "numeric", **summary})
            elif question.kind in {"single_choice", "multiple_choice"}:
                counts = {}
                for answer in answers:
                    counts[answer.value] = counts.get(answer.value, 0) + 1
                charts.append(
                    {
                        "question": {"title": question.title},
                        "type": "choice",
                        "counts": [{"value": key, "total": total} for key, total in sorted(counts.items(), key=lambda item: item[1], reverse=True)],
                    }
                )

            if question.data_type == "continuous":
                analysis = "適合做平均數、標準差與趨勢檢視；若搭配名目分組題，可延伸到 t 檢定與 ANOVA。"
            elif question.data_type == "discrete":
                analysis = "適合做計數型數值摘要，例如總數、平均次數與分布；第一版不自動進入 t 檢定或 ANOVA。"
            elif question.data_type == "nominal":
                analysis = "適合做比例分布與交叉分析；單選名目題可作為推論統計的分組變數。"
            elif question.data_type == "ordinal":
                analysis = "適合做次數、比例與排序分布；因間距不一定相等，第一版不進入 t 檢定或 ANOVA。"
            elif question.data_type == "text":
                analysis = "適合做關鍵字、情緒傾向與主題聚類，提取具體改善線索。"
            else:
                analysis = "建議先確認資料尺度，再選擇描述統計或推論統計方法。"

            question_analysis.append(
                {
                    "title": question.title,
                    "data_type": DATA_TYPE_LABELS.get(question.data_type, question.data_type),
                    "analysis": analysis,
                }
            )

        return jsonify({"charts": charts, "question_analysis": question_analysis})


@app.get("/api/text-analysis")
def text_analysis():
    slug = request.args.get("survey")
    with session_scope() as session:
        survey = session.scalar(select(Survey).where(Survey.slug == slug)) if slug else None
        if not survey:
            return jsonify({"keywords": []})
        question_ids = [
            item.id
            for item in session.scalars(
                select(Question).where(Question.survey_id == survey.id, Question.enable_keyword_tracking.is_(True))
            ).all()
        ]
        answer_rows = (
            session.execute(
                select(Answer.analysis_text, Answer.value, Answer.sentiment_score).where(Answer.question_id.in_(question_ids))
            ).all()
            if question_ids
            else []
        )
        values = [row.analysis_text or row.value for row in answer_rows if (row.analysis_text or row.value)]
        sentiment_values = [row.sentiment_score for row in answer_rows if row.sentiment_score is not None]
        category_rows = session.scalars(select(KeywordCategory).where(KeywordCategory.survey_id == survey.id)).all()
        category_map = {row.keyword: row.category for row in category_rows}
        summary = {
            "total_answers": len(answer_rows),
            "analyzed_answers": sum(1 for row in answer_rows if row.analysis_text),
            "analysis_coverage": round(sum(1 for row in answer_rows if row.analysis_text) / len(answer_rows), 3)
            if answer_rows
            else 0,
            "avg_sentiment_score": round(sum(sentiment_values) / len(sentiment_values), 3) if sentiment_values else None,
        }
        category_sentiments = build_category_sentiments(answer_rows, category_map)
        return jsonify({"keywords": summarize_keywords(values, category_map), "summary": summary, "category_sentiments": category_sentiments})


@app.post("/api/surveys/<slug>/submissions")
def create_submission(slug: str):
    payload = request.get_json(force=True)
    with session_scope() as session:
        survey = session.scalar(select(Survey).where(Survey.slug == slug))
        if not survey:
            return jsonify({"message": "Survey not found"}), 404

        submission = FeedbackSubmission(
            survey_id=survey.id,
            user_id=payload.get("user_id"),
            respondent_name=payload.get("respondent_name", ""),
            respondent_email=payload.get("respondent_email", ""),
            consent_follow_up=bool(payload.get("consent_follow_up", False)),
            submitted_at=datetime.now(),
        )
        session.add(submission)
        session.flush()

        answers = payload.get("answers", {})
        questions = session.scalars(select(Question).where(Question.survey_id == survey.id)).all()
        for question in questions:
            key = f"question_{question.id}"
            if key not in answers:
                continue
            value = answers[key]
            if isinstance(value, list):
                value = ", ".join(value)
            analysis_text = None
            analysis_version = None
            if question.kind in {"short_text", "long_text"}:
                analysis_text = build_analysis_text(value)
                analysis_version = ANALYSIS_VERSION if analysis_text else None
            sentiment_score = estimate_sentiment_score(value) if analysis_text else None
            session.add(
                Answer(
                    submission_id=submission.id,
                    question_id=question.id,
                    value=str(value),
                    analysis_text=analysis_text,
                    sentiment_score=sentiment_score,
                    analysis_version=analysis_version,
                )
            )

        return jsonify(
            {
                "submission_id": submission.id,
                "thank_you_email_enabled": survey.thank_you_email_enabled,
                "respondent_email": submission.respondent_email,
                "survey_title": survey.title,
            }
        ), 201
