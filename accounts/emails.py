import logging

from django.core.mail import send_mail
from django.urls import reverse

from .tokens import make_verification_token

logger = logging.getLogger(__name__)


def send_verification_email(user, request):
    token = make_verification_token(user)
    url = request.build_absolute_uri(reverse("accounts:verify-email", args=[token]))
    subject = "驗證你的 FeedBack IQ 帳號"
    body = (
        f"嗨 {user.first_name or user.username}，\n\n"
        "感謝你註冊 FeedBack IQ！\n\n"
        "請在 72 小時內點擊以下連結完成信箱驗證：\n\n"
        f"{url}\n\n"
        "若你未曾申請此帳號，請忽略這封郵件。\n\n"
        "FeedBack IQ 團隊"
    )
    try:
        send_mail(subject, body, None, [user.email], fail_silently=False)
    except Exception:
        logger.warning("無法寄送驗證信給 %s（SMTP 可能未設定）", user.email)
