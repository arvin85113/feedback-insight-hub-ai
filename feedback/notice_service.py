from dataclasses import dataclass
import smtplib
import socket

from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from django.utils.crypto import salted_hmac

from .models import FeedbackSubmission, ImprovementDispatch, ImprovementNotice, ImprovementUpdate


@dataclass(frozen=True, repr=False)
class NoticeRecipient:
    email: str
    recipient_key: str
    user_id: int | None
    submission_id: int | None


class NoticeConfirmationError(ValueError):
    pass


class NoticeRecipientError(NoticeConfirmationError):
    pass


def _normalized_email(value):
    email = (value or "").strip()
    if not email:
        return None
    try:
        validate_email(email)
    except ValidationError:
        return None
    return email


def _recipient_key(email):
    return salted_hmac("feedback.improvement-notice.recipient", email.casefold()).hexdigest()


def _recipient(email, *, user_id=None, submission_id=None):
    normalized = _normalized_email(email)
    if normalized is None:
        return None
    return NoticeRecipient(
        email=normalized,
        recipient_key=_recipient_key(normalized),
        user_id=user_id,
        submission_id=submission_id,
    )


def _deduplicate(recipients):
    selected = []
    seen = set()
    for recipient in recipients:
        if recipient is None or recipient.recipient_key in seen:
            continue
        seen.add(recipient.recipient_key)
        selected.append(recipient)
    return tuple(selected)


def resolve_global_recipients():
    User = get_user_model()
    users = User.objects.filter(
        role=User.Role.CUSTOMER,
        is_active=True,
        is_email_verified=True,
        notification_opt_in=True,
    ).exclude(email="").order_by("id")
    return _deduplicate(
        _recipient(user.email, user_id=user.id)
        for user in users.iterator()
    )


def resolve_survey_respondents(survey):
    if survey is None:
        return ()
    submissions = (
        FeedbackSubmission.objects.filter(
            survey=survey,
            consent_follow_up=True,
        )
        .select_related("user")
        .order_by("-submitted_at", "-id")
    )
    recipients = []
    for submission in submissions.iterator():
        user = submission.user
        if user is not None:
            if not (
                user.is_active
                and user.is_email_verified
                and user.notification_opt_in
            ):
                continue
            recipients.append(
                _recipient(
                    user.email,
                    user_id=user.id,
                    submission_id=submission.id,
                )
            )
        else:
            recipients.append(
                _recipient(
                    submission.respondent_email,
                    submission_id=submission.id,
                )
            )
    return _deduplicate(recipients)


def resolve_notice_recipients(notice):
    if notice.audience_type == ImprovementNotice.AudienceType.GLOBAL:
        return resolve_global_recipients()
    if notice.audience_type == ImprovementNotice.AudienceType.SURVEY_RESPONDENTS:
        return resolve_survey_respondents(notice.improvement.survey)
    raise ValueError("不支援的通知對象類型。")


@transaction.atomic
def prepare_notice_dispatches(notice_id, *, confirmation_token, content_version, actor):
    notice = (
        ImprovementNotice.objects.select_for_update(of=("self",))
        .select_related("improvement", "improvement__survey")
        .get(pk=notice_id)
    )
    if notice.status != ImprovementNotice.Status.DRAFT:
        return notice, False
    if notice.confirmation_token != confirmation_token or notice.content_version != content_version:
        raise NoticeConfirmationError("通知內容已更新，請重新預覽後再確認。")

    recipients = resolve_notice_recipients(notice)
    if not recipients:
        raise NoticeRecipientError("目前沒有符合條件的收件者，未建立寄送紀錄。")

    ImprovementDispatch.objects.bulk_create(
        [
            ImprovementDispatch(
                improvement=notice.improvement,
                notice=notice,
                submission_id=recipient.submission_id,
                recipient_user_id=recipient.user_id,
                recipient_key=recipient.recipient_key,
                delivery_status=ImprovementDispatch.DeliveryStatus.PENDING,
            )
            for recipient in recipients
        ]
    )
    notice.status = ImprovementNotice.Status.SENDING
    notice.recipient_count = len(recipients)
    notice.sent_count = 0
    notice.failed_count = 0
    notice.confirmed_by = actor
    notice.confirmed_at = timezone.now()
    notice.sent_at = None
    notice.last_error_code = ""
    notice.save(
        update_fields=[
            "status",
            "recipient_count",
            "sent_count",
            "failed_count",
            "confirmed_by",
            "confirmed_at",
            "sent_at",
            "last_error_code",
            "updated_at",
        ]
    )
    return notice, True


def _current_dispatch_email(dispatch):
    if (
        dispatch.notice.audience_type == ImprovementNotice.AudienceType.SURVEY_RESPONDENTS
        and (dispatch.submission is None or not dispatch.submission.consent_follow_up)
    ):
        return None
    user = dispatch.recipient_user
    if user is not None:
        if not (
            user.role == user.Role.CUSTOMER
            and user.is_active
            and user.is_email_verified
            and user.notification_opt_in
        ):
            return None
        return _normalized_email(user.email)

    submission = dispatch.submission
    if submission is None or not submission.consent_follow_up:
        return None
    return _normalized_email(submission.respondent_email)


@transaction.atomic
def _claim_dispatch(dispatch_id, *, retry_failed):
    dispatch = (
        ImprovementDispatch.objects.select_for_update(of=("self",))
        .select_related("notice", "recipient_user", "submission")
        .get(pk=dispatch_id)
    )
    expected_status = (
        ImprovementDispatch.DeliveryStatus.FAILED
        if retry_failed
        else ImprovementDispatch.DeliveryStatus.PENDING
    )
    if dispatch.delivery_status != expected_status:
        return None
    dispatch.delivery_status = ImprovementDispatch.DeliveryStatus.SENDING
    dispatch.attempt_count += 1
    dispatch.last_attempt_at = timezone.now()
    dispatch.error_code = ""
    dispatch.save(
        update_fields=[
            "delivery_status",
            "attempt_count",
            "last_attempt_at",
            "error_code",
        ]
    )
    return {
        "dispatch_id": dispatch.id,
        "email": _current_dispatch_email(dispatch),
        "subject": dispatch.notice.subject,
        "body": dispatch.notice.body,
    }


def _delivery_error_code(exc):
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "authentication_error"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "recipient_rejected"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, (smtplib.SMTPServerDisconnected, ConnectionError)):
        return "provider_unavailable"
    if isinstance(exc, smtplib.SMTPException):
        return "smtp_error"
    return "email_error"


@transaction.atomic
def _mark_dispatch_sent(dispatch_id):
    dispatch = ImprovementDispatch.objects.select_for_update().get(pk=dispatch_id)
    if dispatch.delivery_status != ImprovementDispatch.DeliveryStatus.SENDING:
        return
    dispatch.delivery_status = ImprovementDispatch.DeliveryStatus.SENT
    dispatch.sent_at = timezone.now()
    dispatch.error_code = ""
    dispatch.save(update_fields=["delivery_status", "sent_at", "error_code"])


@transaction.atomic
def _mark_dispatch_failed(dispatch_id, error_code):
    dispatch = ImprovementDispatch.objects.select_for_update().get(pk=dispatch_id)
    if dispatch.delivery_status != ImprovementDispatch.DeliveryStatus.SENDING:
        return
    dispatch.delivery_status = ImprovementDispatch.DeliveryStatus.FAILED
    dispatch.sent_at = None
    dispatch.error_code = error_code
    dispatch.save(update_fields=["delivery_status", "sent_at", "error_code"])


@transaction.atomic
def _finalize_notice(notice_id):
    notice = ImprovementNotice.objects.select_for_update().get(pk=notice_id)
    counts = {
        row["delivery_status"]: row["total"]
        for row in notice.dispatches.values("delivery_status").annotate(total=Count("id"))
    }
    sent_count = counts.get(ImprovementDispatch.DeliveryStatus.SENT, 0)
    failed_count = counts.get(ImprovementDispatch.DeliveryStatus.FAILED, 0)
    unfinished_count = sum(
        counts.get(status, 0)
        for status in (
            ImprovementDispatch.DeliveryStatus.PENDING,
            ImprovementDispatch.DeliveryStatus.SENDING,
        )
    )
    notice.sent_count = sent_count
    notice.failed_count = failed_count
    if unfinished_count:
        notice.status = ImprovementNotice.Status.SENDING
        notice.sent_at = None
    elif failed_count and sent_count:
        notice.status = ImprovementNotice.Status.PARTIALLY_SENT
        notice.sent_at = timezone.now()
    elif failed_count:
        notice.status = ImprovementNotice.Status.FAILED
        notice.sent_at = timezone.now()
    else:
        notice.status = ImprovementNotice.Status.SENT
        notice.sent_at = timezone.now()
    first_error = (
        notice.dispatches.filter(delivery_status=ImprovementDispatch.DeliveryStatus.FAILED)
        .exclude(error_code="")
        .values_list("error_code", flat=True)
        .first()
    )
    notice.last_error_code = first_error or ""
    notice.save(
        update_fields=[
            "status",
            "sent_count",
            "failed_count",
            "sent_at",
            "last_error_code",
            "updated_at",
        ]
    )
    if sent_count:
        ImprovementUpdate.objects.filter(pk=notice.improvement_id, emailed_at__isnull=True).update(
            emailed_at=notice.sent_at or timezone.now()
        )
    return notice


def send_notice_batch(notice_id, *, retry_failed=False):
    delivery_status = (
        ImprovementDispatch.DeliveryStatus.FAILED
        if retry_failed
        else ImprovementDispatch.DeliveryStatus.PENDING
    )
    dispatch_ids = list(
        ImprovementDispatch.objects.filter(
            notice_id=notice_id,
            delivery_status=delivery_status,
        ).values_list("id", flat=True)
    )
    for dispatch_id in dispatch_ids:
        claimed = _claim_dispatch(dispatch_id, retry_failed=retry_failed)
        if claimed is None:
            continue
        if claimed["email"] is None:
            _mark_dispatch_failed(dispatch_id, "recipient_ineligible")
            continue
        try:
            sent = send_mail(
                subject=claimed["subject"],
                message=claimed["body"],
                from_email=None,
                recipient_list=[claimed["email"]],
                fail_silently=False,
            )
            if sent != 1:
                raise RuntimeError("email backend did not accept the message")
        except Exception as exc:
            _mark_dispatch_failed(dispatch_id, _delivery_error_code(exc))
        else:
            _mark_dispatch_sent(dispatch_id)
    return _finalize_notice(notice_id)


@transaction.atomic
def begin_notice_retry(notice_id):
    notice = ImprovementNotice.objects.select_for_update().get(pk=notice_id)
    if notice.status not in {
        ImprovementNotice.Status.FAILED,
        ImprovementNotice.Status.PARTIALLY_SENT,
    }:
        return notice, False
    if not notice.dispatches.filter(delivery_status=ImprovementDispatch.DeliveryStatus.FAILED).exists():
        return notice, False
    notice.status = ImprovementNotice.Status.SENDING
    notice.sent_at = None
    notice.save(update_fields=["status", "sent_at", "updated_at"])
    return notice, True
