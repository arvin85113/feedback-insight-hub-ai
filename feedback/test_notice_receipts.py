import uuid
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from cloudapi.models import NodeDevice, SubmissionReceipt
from cloudapi.writes import assign_survey_to_node
from feedback.models import FeedbackSubmission, ImprovementDispatch, ImprovementNotice, ImprovementUpdate, Survey
from feedback.notice_service import (
    NoticeRecipientError,
    prepare_notice_dispatches,
    resolve_survey_respondents,
    send_notice_batch,
)
from feedback.test_utils import cloud_only


@cloud_only
class ReceiptRecipientTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(username="rm", password="x", role=User.Role.MANAGER)
        self.node, _ = NodeDevice.issue("office")
        self.survey = assign_survey_to_node(Survey.objects.create(title="S", slug="s"), self.node).survey
        self.improvement = ImprovementUpdate.objects.create(
            survey=self.survey, title="t", summary="s", created_by=self.manager, updated_by=self.manager)
        self.customer = User.objects.create_user(
            username="rc", password="x", role=User.Role.CUSTOMER, email="rc@example.com",
            is_email_verified=True, notification_opt_in=True)
        self.sequence = 0

    def receipt(self, **overrides):
        self.sequence += 1
        fields = dict(
            submission_uuid=uuid.uuid4(), node=self.node, survey=self.survey, submitted_at=timezone.now(),
            definition_version=1, response_sequence=self.sequence, payload_hash="0" * 64,
            status="received", user=self.customer, consent_follow_up=True)
        fields.update(overrides)
        return SubmissionReceipt.objects.create(**fields)

    def notice(self):
        return ImprovementNotice.objects.create(
            improvement=self.improvement, subject="s", body="b",
            audience_type=ImprovementNotice.AudienceType.SURVEY_RESPONDENTS, created_by=self.manager)

    def prepare(self, notice):
        return prepare_notice_dispatches(
            notice.pk, confirmation_token=notice.confirmation_token,
            content_version=notice.content_version, actor=self.manager)

    def test_consenting_receipt_is_a_recipient(self):
        self.receipt()
        recipients = resolve_survey_respondents(self.survey)
        self.assertEqual(len(recipients), 1)
        self.assertEqual(recipients[0].user_id, self.customer.id)
        self.assertIsNone(recipients[0].submission_id)

    def test_excluded_receipts(self):
        self.receipt(consent_follow_up=False)
        self.receipt(user=None)
        self.receipt(status="quarantined")
        self.receipt(status="abandoned")
        self.assertEqual(resolve_survey_respondents(self.survey), ())

    def test_receipt_matching_a_submission_counts_once(self):
        key = uuid.uuid4()
        submission = FeedbackSubmission.objects.create(
            survey=self.survey, user=self.customer, respondent_email=self.customer.email,
            consent_follow_up=True, idempotency_key=key)
        self.receipt(submission_uuid=key)
        recipients = resolve_survey_respondents(self.survey)
        self.assertEqual(len(recipients), 1)
        self.assertEqual(recipients[0].submission_id, submission.id)

    @patch("feedback.notice_service.send_mail", return_value=1)
    def test_receipt_dispatch_is_sent(self, send_mail):
        self.receipt()
        notice = self.notice()
        self.prepare(notice)
        send_notice_batch(notice.pk)
        notice.refresh_from_db()
        self.assertEqual(notice.status, ImprovementNotice.Status.SENT)
        dispatch = notice.dispatches.get()
        self.assertEqual(dispatch.recipient_user, self.customer)
        self.assertIsNone(dispatch.submission)
        send_mail.assert_called_once()

    @patch("feedback.notice_service.send_mail", return_value=1)
    def test_recheck_skips_opt_out_and_abandoned(self, send_mail):
        other = User.objects.create_user(
            username="rc2", password="x", role=User.Role.CUSTOMER, email="rc2@example.com",
            is_email_verified=True, notification_opt_in=True)
        self.receipt()
        abandoned = self.receipt(user=other)
        notice = self.notice()
        self.prepare(notice)
        User.objects.filter(pk=self.customer.pk).update(notification_opt_in=False)
        SubmissionReceipt.objects.filter(pk=abandoned.pk).update(status="abandoned")
        send_notice_batch(notice.pk)
        for dispatch in notice.dispatches.all():
            self.assertNotEqual(dispatch.delivery_status, ImprovementDispatch.DeliveryStatus.SENT)
        send_mail.assert_not_called()

    def test_improvement_without_survey_has_no_recipients(self):
        self.receipt()
        notice = self.notice()
        ImprovementUpdate.objects.filter(pk=self.improvement.pk).update(survey=None)
        with self.assertRaises(NoticeRecipientError):
            self.prepare(notice)
