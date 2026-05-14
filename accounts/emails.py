import logging
import os

import requests
from django.urls import reverse

from .tokens import make_verification_token

logger = logging.getLogger(__name__)

SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"


def _send_via_sendgrid(to_email, subject, text_body):
    """Send via SendGrid HTTP API. Returns True on success."""
    api_key = os.getenv("SENDGRID_API_KEY", "")
    if not api_key:
        logger.warning("SENDGRID_API_KEY not set — email not sent")
        return False

    from_email = os.getenv("DEFAULT_FROM_EMAIL", "arvin85113@gmail.com")
    # Strip display name if present (e.g. "FeedBack IQ <foo@bar.com>")
    if "<" in from_email:
        display, addr = from_email.split("<", 1)
        from_obj = {"email": addr.strip().rstrip(">"), "name": display.strip()}
    else:
        from_obj = {"email": from_email}

    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": from_obj,
        "subject": subject,
        "content": [{"type": "text/plain", "value": text_body}],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(SENDGRID_API_URL, json=payload, headers=headers, timeout=10)
        if resp.status_code == 202:
            return True
        logger.error("SendGrid send failed for %s: %s %s", to_email, resp.status_code, resp.text)
        return False
    except Exception as exc:
        logger.error("SendGrid request error for %s: %s", to_email, exc)
        return False


def send_verification_email(user, request):
    token = make_verification_token(user)
    url = request.build_absolute_uri(reverse("accounts:verify-email", args=[token]))
    subject = "驗證你的 FeedBack IQ 帳號"
    text_body = (
        f"嗨 {user.first_name or user.username}，\n\n"
        "感謝你加入 FeedBack IQ！\n\n"
        f"請在 72 小時內點擊以下連結完成信箱驗證：\n\n"
        f"{url}\n\n"
        "若你未曾申請此帳號，請忽略這封郵件。\n\n"
        "FeedBack IQ 團隊"
    )
    _send_via_sendgrid(user.email, subject, text_body)


def send_password_reset_email_via_resend(to_email, reset_url, site_name="FeedBack IQ"):
    """Called by SafePasswordResetForm — now uses SendGrid under the hood."""
    subject = f"重設你的 {site_name} 密碼"
    text_body = (
        f"你收到這封信是因為有人申請重設 {site_name} 的密碼。\n\n"
        f"請點擊以下連結重設密碼（連結有效期 24 小時）：\n\n"
        f"{reset_url}\n\n"
        "若你未曾提出此申請，請忽略這封郵件，密碼不會被更改。\n\n"
        f"{site_name} 團隊"
    )
    return _send_via_sendgrid(to_email, subject, text_body)
