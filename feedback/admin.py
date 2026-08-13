from django.contrib import admin

from .models import (
    Answer,
    FeedbackSubmission,
    ImprovementDispatch,
    ImprovementNotice,
    ImprovementStatusHistory,
    ImprovementUpdate,
    KeywordCategory,
    Question,
    Survey,
    SurveyCategory,
)


class QuestionInline(admin.TabularInline):
    model = Question
    extra = 1


@admin.register(SurveyCategory)
class SurveyCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at")


@admin.register(Survey)
class SurveyAdmin(admin.ModelAdmin):
    list_display = ("title", "slug", "is_active", "updated_at")
    prepopulated_fields = {"slug": ("title",)}
    inlines = [QuestionInline]
    readonly_fields = ("improvement_tracking_enabled",)
    fields = (
        "title", "slug", "description",
        "category",
        "thank_you_email_enabled", "is_active",
        "improvement_tracking_enabled",
    )


@admin.register(FeedbackSubmission)
class FeedbackSubmissionAdmin(admin.ModelAdmin):
    list_display = ("survey", "display_name", "respondent_email", "submitted_at")
    list_filter = ("survey", "consent_follow_up")


@admin.register(Answer)
class AnswerAdmin(admin.ModelAdmin):
    list_display = ("question", "submission", "value")


admin.site.register(KeywordCategory)

@admin.register(ImprovementUpdate)
class ImprovementUpdateAdmin(admin.ModelAdmin):
    list_display = ("title", "survey", "status", "priority", "updated_at")
    list_filter = ("status", "priority", "survey")
    readonly_fields = (
        "survey",
        "send_global_notice",
        "status",
        "source_ai_analysis_stage",
        "source_ai_draft_id",
        "source_evidence_refs",
        "source_ai_metadata",
        "created_by",
        "updated_by",
        "created_at",
        "updated_at",
        "completed_at",
        "archived_at",
        "emailed_at",
    )

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ImprovementStatusHistory)
class ImprovementStatusHistoryAdmin(admin.ModelAdmin):
    list_display = ("improvement", "from_status", "to_status", "changed_by", "changed_at")
    readonly_fields = ("improvement", "from_status", "to_status", "changed_by", "changed_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ImprovementNotice)
class ImprovementNoticeAdmin(admin.ModelAdmin):
    list_display = ("subject", "improvement", "audience_type", "status", "recipient_count", "sent_at")
    list_filter = ("status", "audience_type")
    readonly_fields = (
        "status",
        "recipient_count",
        "sent_count",
        "failed_count",
        "created_by",
        "confirmed_by",
        "confirmation_token",
        "content_version",
        "last_error_code",
        "created_at",
        "updated_at",
        "confirmed_at",
        "sent_at",
    )

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status != ImprovementNotice.Status.DRAFT:
            return tuple(field.name for field in obj._meta.fields)
        return self.readonly_fields

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ImprovementDispatch)
class ImprovementDispatchAdmin(admin.ModelAdmin):
    list_display = ("notice", "improvement", "delivery_status", "attempt_count", "sent_at", "is_read")
    list_filter = ("delivery_status", "is_read")
    readonly_fields = (
        "notice",
        "improvement",
        "submission",
        "recipient_user",
        "recipient_key",
        "personalized_note",
        "delivery_status",
        "attempt_count",
        "last_attempt_at",
        "error_code",
        "sent_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
