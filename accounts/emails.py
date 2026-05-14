import logging
import os

from django.urls import reverse

from .tokens import make_verification_token

logger = logging.getLogger(__name__)


def _send_via_resend(to_email, subject, html_body, text_body):
    """Send via Resend HTTP API. Returns True on success."""
    import resend
    api_key = os.getenv("RESEND_API_KEY", "")
    if not api_key:
        logger.warning("RESEND_API_KEY not set — email not sent")
        return False
    resend.api_key = api_key
    from_addr = os.getenv("DEFAULT_FROM_EMAIL", "FeedBack IQ <onboarding@resend.dev>")
    try:
        resend.Emails.send({
            "from": from_addr,
            "to": [to_email],
            "subject": subject,
            "text": text_body,
        })
        return True
    except Exception as exc:
        logger.error("Resend send failed for %s: %s", to_email, exc)
        return False


def send_verification_email(user, request):
    token = make_verification_token(user)
    url = request.build_absolute_uri(reverse("accounts:verify-email", args=[token]))
    subject = "驗證你的 FeedBack IQ 帳號"
    text_body = (
        f"嗨 {user.first_name or user.username}，\n\n"
        "感謝你註冊 FeedBack IQ！\n\n"
        "請在 72 小時內點擊以下連結完成信箱驗證：\n\n"
        f"{url}\n\n"
        "若你未曾申請此帳號，請忽略這封郵件。\n\n"
        "FeedBack IQ 團隊"
    )
    _send_via_resend(user.email, subject, None, text_body)


def send_password_reset_email_via_resend(to_email, reset_url, site_name="FeedBack IQ"):
    """Called by SafePasswordResetForm if RESEND_API_KEY is set."""
    subject = f"重設你的 {site_name} 密碼"
    text_body = (
        f"你收到這封信是因為有人申請重設 {site_name} 的密碼。\n\n"
        f"請點擊以下連結重設密碼（連結有效期 24 小時）：\n\n"
        f"{reset_url}\n\n"
        "若你未曾提出此申請，請忽略這封郵件，密碼不會被更改。\n\n"
        f"{site_name} 團隊"
    )
    return _send_via_resend(to_email, subject, None, text_body)
