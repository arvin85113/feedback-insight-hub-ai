import uuid

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "accounts_user"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(150))
    first_name: Mapped[str] = mapped_column(String(150))
    last_name: Mapped[str] = mapped_column(String(150))
    email: Mapped[str] = mapped_column(String(254))
    organization: Mapped[str | None] = mapped_column(String(255))
    notification_opt_in: Mapped[bool] = mapped_column(Boolean, default=True)
    is_email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    role: Mapped[str] = mapped_column(String(20))
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False)


class SurveyCategory(Base):
    __tablename__ = "feedback_surveycategory"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime)

    surveys: Mapped[list["Survey"]] = relationship(back_populates="category")


class Survey(Base):
    __tablename__ = "feedback_survey"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(50), unique=True)
    description: Mapped[str] = mapped_column(Text)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("feedback_surveycategory.id"), nullable=True)
    thank_you_email_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    improvement_tracking_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime)
    updated_at: Mapped[DateTime] = mapped_column(DateTime)

    category: Mapped["SurveyCategory | None"] = relationship(back_populates="surveys")
    questions: Mapped[list["Question"]] = relationship(order_by="Question.order", back_populates="survey")
    submissions: Mapped[list["FeedbackSubmission"]] = relationship(back_populates="survey")
    improvements: Mapped[list["ImprovementUpdate"]] = relationship(back_populates="survey")


class Question(Base):
    __tablename__ = "feedback_question"

    id: Mapped[int] = mapped_column(primary_key=True)
    survey_id: Mapped[int] = mapped_column(ForeignKey("feedback_survey.id"))
    title: Mapped[str] = mapped_column(String(255))
    help_text: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(20))
    data_type: Mapped[str] = mapped_column(String(20))
    options_text: Mapped[str] = mapped_column(Text)
    is_required: Mapped[bool] = mapped_column(Boolean, default=True)
    enable_keyword_tracking: Mapped[bool] = mapped_column(Boolean, default=False)
    order: Mapped[int] = mapped_column(Integer, default=1)

    survey: Mapped["Survey"] = relationship(back_populates="questions")


class FeedbackSubmission(Base):
    __tablename__ = "feedback_feedbacksubmission"

    id: Mapped[int] = mapped_column(primary_key=True)
    survey_id: Mapped[int] = mapped_column(ForeignKey("feedback_survey.id"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("accounts_user.id"), nullable=True)
    respondent_name: Mapped[str] = mapped_column(String(120))
    respondent_email: Mapped[str] = mapped_column(String(254))
    consent_follow_up: Mapped[bool] = mapped_column(Boolean, default=False)
    submitted_at: Mapped[DateTime] = mapped_column(DateTime)

    survey: Mapped["Survey"] = relationship(back_populates="submissions")
    user: Mapped["User | None"] = relationship()
    answers: Mapped[list["Answer"]] = relationship()


class Answer(Base):
    __tablename__ = "feedback_answer"

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("feedback_feedbacksubmission.id"))
    question_id: Mapped[int] = mapped_column(ForeignKey("feedback_question.id"))
    value: Mapped[str] = mapped_column(Text)
    analysis_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    sentiment_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    analysis_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    question: Mapped["Question"] = relationship()


class SurveyAIReportSnapshot(Base):
    __tablename__ = "feedback_survey_ai_report_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    survey_id: Mapped[int] = mapped_column(ForeignKey("feedback_survey.id"))
    data_fingerprint: Mapped[str] = mapped_column(String(64))
    snapshot_schema_version: Mapped[str] = mapped_column(String(32))
    prompt_version: Mapped[str] = mapped_column(String(32))
    model_name: Mapped[str] = mapped_column(String(100))
    source_snapshot: Mapped[dict] = mapped_column(JSON)
    ai_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(24))
    response_count: Mapped[int] = mapped_column(Integer, default=0)
    analysis_coverage: Mapped[float] = mapped_column(Float, default=0)
    source_latest_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)
    generated_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)
    snapshot_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    generation_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fingerprint_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[DateTime] = mapped_column(DateTime)
    updated_at: Mapped[DateTime] = mapped_column(DateTime)


class SurveyAIAnalysisStage(Base):
    __tablename__ = "feedback_survey_ai_analysis_stages"

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("feedback_survey_ai_report_snapshots.id", ondelete="CASCADE")
    )
    stage_type: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    input_hash: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(32))
    prompt_version: Mapped[str] = mapped_column(String(32))
    model_name: Mapped[str] = mapped_column(String(100))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    input_manifest: Mapped[dict] = mapped_column(JSON)
    output_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str] = mapped_column(String(64), default="")
    generation_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_metrics: Mapped[dict] = mapped_column(JSON)
    reused_from_id: Mapped[int | None] = mapped_column(
        ForeignKey("feedback_survey_ai_analysis_stages.id", ondelete="SET NULL"),
        nullable=True,
    )
    generated_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime)
    updated_at: Mapped[DateTime] = mapped_column(DateTime)


class ImprovementUpdate(Base):
    __tablename__ = "feedback_improvementupdate"

    id: Mapped[int] = mapped_column(primary_key=True)
    survey_id: Mapped[int | None] = mapped_column(
        ForeignKey("feedback_survey.id", ondelete="SET NULL"),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text)
    related_category: Mapped[str] = mapped_column(String(100))
    send_global_notice: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    priority: Mapped[str] = mapped_column(String(12), default="medium")
    due_date: Mapped[Date | None] = mapped_column(Date, nullable=True)
    internal_note: Mapped[str] = mapped_column(Text, default="")
    source_ai_analysis_stage_id: Mapped[int | None] = mapped_column(
        ForeignKey("feedback_survey_ai_analysis_stages.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_ai_draft_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_evidence_refs: Mapped[list | None] = mapped_column(JSON, nullable=True)
    source_ai_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("accounts_user.id"), nullable=True)
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("accounts_user.id"), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime)
    updated_at: Mapped[DateTime] = mapped_column(DateTime)
    completed_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)
    archived_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)
    emailed_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)

    survey: Mapped[Survey | None] = relationship(back_populates="improvements")


class ImprovementStatusHistory(Base):
    __tablename__ = "feedback_improvementstatushistory"

    id: Mapped[int] = mapped_column(primary_key=True)
    improvement_id: Mapped[int] = mapped_column(ForeignKey("feedback_improvementupdate.id"))
    from_status: Mapped[str] = mapped_column(String(20), default="")
    to_status: Mapped[str] = mapped_column(String(20))
    changed_by_id: Mapped[int | None] = mapped_column(ForeignKey("accounts_user.id"), nullable=True)
    changed_at: Mapped[DateTime] = mapped_column(DateTime)


class KeywordCategory(Base):
    __tablename__ = "feedback_keywordcategory"

    id: Mapped[int] = mapped_column(primary_key=True)
    survey_id: Mapped[int] = mapped_column(ForeignKey("feedback_survey.id"))
    keyword: Mapped[str] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(100))
    threshold: Mapped[int] = mapped_column(Integer, default=2)


class ImprovementNotice(Base):
    __tablename__ = "feedback_improvementnotice"

    id: Mapped[int] = mapped_column(primary_key=True)
    improvement_id: Mapped[int] = mapped_column(ForeignKey("feedback_improvementupdate.id"))
    subject: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    audience_type: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24), default="draft")
    recipient_count: Mapped[int] = mapped_column(Integer, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("accounts_user.id"), nullable=True)
    confirmed_by_id: Mapped[int | None] = mapped_column(ForeignKey("accounts_user.id"), nullable=True)
    confirmation_token: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True))
    content_version: Mapped[int] = mapped_column(Integer, default=1)
    last_error_code: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[DateTime] = mapped_column(DateTime)
    updated_at: Mapped[DateTime] = mapped_column(DateTime)
    confirmed_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)

    improvement: Mapped["ImprovementUpdate"] = relationship()


class ImprovementDispatch(Base):
    __tablename__ = "feedback_improvementdispatch"

    id: Mapped[int] = mapped_column(primary_key=True)
    improvement_id: Mapped[int] = mapped_column(ForeignKey("feedback_improvementupdate.id"))
    notice_id: Mapped[int | None] = mapped_column(ForeignKey("feedback_improvementnotice.id"), nullable=True)
    submission_id: Mapped[int | None] = mapped_column(ForeignKey("feedback_feedbacksubmission.id"), nullable=True)
    recipient_user_id: Mapped[int | None] = mapped_column(ForeignKey("accounts_user.id"), nullable=True)
    recipient_key: Mapped[str] = mapped_column(String(64), default="")
    personalized_note: Mapped[str] = mapped_column(Text)
    delivery_status: Mapped[str] = mapped_column(String(16), default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[str] = mapped_column(String(64), default="")
    sent_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)

    improvement: Mapped["ImprovementUpdate"] = relationship()
    notice: Mapped["ImprovementNotice | None"] = relationship()
    submission: Mapped["FeedbackSubmission | None"] = relationship()
    recipient_user: Mapped["User | None"] = relationship()
