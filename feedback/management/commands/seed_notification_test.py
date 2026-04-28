from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from accounts.models import User
from feedback.models import (
    Answer,
    FeedbackSubmission,
    ImprovementDispatch,
    ImprovementUpdate,
    Survey,
)

# 四種通知設定的測試用戶
# 注意：notification_channel 欄位尚未建立，inapp/email/both 三者目前行為相同（notification_opt_in=True）
# 待 notification_channel 功能上線後，可更新這四位用戶的 channel 設定進行細分測試
TEST_USERS = [
    {
        "username": "test_inapp",
        "first_name": "帳號通知",
        "email": "test_inapp@example.com",
        "notification_opt_in": True,
        "consent_follow_up": True,
        "label": "只要帳號通知",
    },
    {
        "username": "test_email_only",
        "first_name": "Email通知",
        "email": "test_email@example.com",
        "notification_opt_in": True,
        "consent_follow_up": True,
        "label": "只要Email",
    },
    {
        "username": "test_both",
        "first_name": "全部通知",
        "email": "test_both@example.com",
        "notification_opt_in": True,
        "consent_follow_up": True,
        "label": "兩者都要",
    },
    {
        "username": "test_none",
        "first_name": "不接收通知",
        "email": "test_none@example.com",
        "notification_opt_in": False,
        "consent_follow_up": False,
        "label": "都不要",
    },
]

# 對應 seed_demo 問卷題目順序的範例答案
SAMPLE_ANSWERS = [
    "8",                                            # 整體滿意度（量表）
    "通知追蹤",                                      # 最常使用的功能（單選）
    "符合",                                          # 回應速度（單選）
    "希望介面更直覺，速度也可以更快，通知功能很實用。",  # 改善建議（長文字）
]


class Command(BaseCommand):
    help = "建立通知功能測試用資料（測試用戶、問卷回覆、改善派送）"

    def handle(self, *args, **options):
        survey = Survey.objects.filter(slug="product-feedback").first()
        if not survey:
            self.stdout.write(self.style.ERROR(
                "找不到示範問卷，請先執行 python manage.py seed_demo"
            ))
            return

        questions = list(survey.questions.order_by("order"))
        if not questions:
            self.stdout.write(self.style.ERROR("示範問卷沒有題目，請先確認 seed_demo 執行正確"))
            return

        self.stdout.write("建立測試用戶與問卷回覆...")
        created_records = []

        for spec in TEST_USERS:
            # 建立或更新用戶
            user, user_created = User.objects.get_or_create(
                username=spec["username"],
                defaults={
                    "first_name": spec["first_name"],
                    "email": spec["email"],
                    "notification_opt_in": spec["notification_opt_in"],
                    "role": User.Role.CUSTOMER,
                },
            )
            if not user_created:
                user.email = spec["email"]
                user.notification_opt_in = spec["notification_opt_in"]
                user.save(update_fields=["email", "notification_opt_in"])
            user.set_password("testpass1234")
            user.save(update_fields=["password"])

            # 建立問卷回覆
            submission, _ = FeedbackSubmission.objects.get_or_create(
                survey=survey,
                user=user,
                defaults={
                    "respondent_name": spec["first_name"],
                    "respondent_email": spec["email"],
                    "consent_follow_up": spec["consent_follow_up"],
                    "source": Survey.AccessMode.LOGIN,
                },
            )

            # 填寫答案
            for question, value in zip(questions, SAMPLE_ANSWERS):
                Answer.objects.get_or_create(
                    submission=submission,
                    question=question,
                    defaults={"value": value},
                )

            action = "建立" if user_created else "更新"
            self.stdout.write(f"  [{spec['label']:10s}] {user.username} ({action})")
            created_records.append((user, submission, spec["label"]))

        # 建立改善項目
        self.stdout.write("\n建立改善項目...")
        improvement, imp_created = ImprovementUpdate.objects.get_or_create(
            survey=survey,
            title="通知測試：介面速度優化",
            defaults={
                "summary": "根據用戶回饋，已針對系統回應速度與介面導覽進行優化，平均載入時間縮短 40%。",
                "related_category": "速度",
                "send_global_notice": True,
            },
        )
        self.stdout.write(f"  {'建立' if imp_created else '已存在'}：{improvement.title}")

        # 派送通知給符合條件的填答者
        self.stdout.write("\n派送通知...")
        recipients = (
            survey.submissions.select_related("user")
            .filter(Q(consent_follow_up=True) | Q(user__notification_opt_in=True))
            .exclude(respondent_email="")
        )

        dispatched = 0
        skipped = 0
        for submission in recipients:
            user = submission.user
            if user and not user.notification_opt_in and not submission.consent_follow_up:
                skipped += 1
                continue
            _, created = ImprovementDispatch.objects.get_or_create(
                improvement=improvement,
                submission=submission,
                defaults={
                    "personalized_note": (
                        f"你先前在「{improvement.related_category or survey.title}」"
                        "提供的回饋已被納入這次改善，感謝你的意見。"
                    ),
                    "sent_at": timezone.now(),
                },
            )
            if created:
                dispatched += 1

        if dispatched > 0:
            improvement.emailed_at = timezone.now()
            improvement.save(update_fields=["emailed_at"])

        # 輸出測試帳號清單
        self.stdout.write(self.style.SUCCESS(
            f"\n完成：{len(created_records)} 位測試用戶，新增 {dispatched} 筆通知派送"
        ))
        self.stdout.write("\n測試帳號一覽（密碼均為 testpass1234）：")
        self.stdout.write(f"  {'帳號':<20} {'設定':<12} {'Email'}")
        self.stdout.write("  " + "-" * 55)
        for user, _, label in created_records:
            self.stdout.write(f"  {user.username:<20} {label:<12} {user.email}")
