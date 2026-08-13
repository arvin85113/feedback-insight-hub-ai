from django.core.management.base import BaseCommand

from accounts.models import User
from feedback.improvement_workflow import record_initial_status
from feedback.models import (
    Answer,
    FeedbackSubmission,
    ImprovementNotice,
    ImprovementUpdate,
    Question,
    Survey,
)


TEST_SURVEY_SLUG = "notification-test-survey"
TEST_SURVEY_TITLE = "通知功能測試問卷"
TEST_USERS = [
    {
        "username": "notice_demo_opted_in_a",
        "email": "notice-demo-a@example.com",
        "notification_opt_in": True,
        "consent_follow_up": True,
    },
    {
        "username": "notice_demo_opted_in_b",
        "email": "notice-demo-b@example.com",
        "notification_opt_in": True,
        "consent_follow_up": True,
    },
    {
        "username": "notice_demo_opted_out",
        "email": "notice-demo-out@example.com",
        "notification_opt_in": False,
        "consent_follow_up": False,
    },
]


class Command(BaseCommand):
    help = "建立通知流程測試資料與未寄送草稿；不會寄送 email"

    def handle(self, *args, **options):
        survey, _ = Survey.objects.update_or_create(
            slug=TEST_SURVEY_SLUG,
            defaults={
                "title": TEST_SURVEY_TITLE,
                "description": "供通知草稿、預覽與明確確認流程測試使用。",
                "thank_you_email_enabled": False,
                "improvement_tracking_enabled": True,
                "is_active": True,
            },
        )
        question, _ = Question.objects.update_or_create(
            survey=survey,
            title="改善建議",
            defaults={
                "kind": Question.Kind.LONG_TEXT,
                "data_type": Question.DataType.TEXT,
                "is_required": False,
                "enable_keyword_tracking": True,
                "order": 1,
            },
        )

        for index, spec in enumerate(TEST_USERS, start=1):
            user, created = User.objects.update_or_create(
                username=spec["username"],
                defaults={
                    "email": spec["email"],
                    "notification_opt_in": spec["notification_opt_in"],
                    "is_email_verified": True,
                    "role": User.Role.CUSTOMER,
                },
            )
            if created:
                user.set_unusable_password()
                user.save(update_fields=["password"])
            submission, _ = FeedbackSubmission.objects.update_or_create(
                survey=survey,
                user=user,
                defaults={
                    "respondent_name": f"通知測試顧客 {index}",
                    "respondent_email": user.email,
                    "consent_follow_up": spec["consent_follow_up"],
                },
            )
            Answer.objects.update_or_create(
                submission=submission,
                question=question,
                defaults={"value": "希望改善通知流程更清楚。"},
            )

        improvement, created = ImprovementUpdate.objects.update_or_create(
            survey=survey,
            title="通知測試：改善通知流程",
            defaults={
                "summary": "將改善項目儲存、通知草稿與實際寄送拆成三個明確步驟。",
                "related_category": "通知流程",
                "send_global_notice": False,
                "status": ImprovementUpdate.Status.PLANNED,
                "priority": ImprovementUpdate.Priority.MEDIUM,
            },
        )
        if created:
            record_initial_status(improvement, None)

        notice, notice_created = ImprovementNotice.objects.get_or_create(
            improvement=improvement,
            subject="通知流程改善進度",
            defaults={
                "body": "我們已將改善項目與通知寄送分離，所有對外通知都需要管理者預覽並確認。",
                "audience_type": ImprovementNotice.AudienceType.SURVEY_RESPONDENTS,
            },
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"通知測試資料已就緒；通知草稿 {'已建立' if notice_created else '已存在'}，未寄送任何 email。"
            )
        )
        self.stdout.write(f"請從改善項目 #{improvement.pk} 或通知草稿 #{notice.pk} 繼續預覽。")
