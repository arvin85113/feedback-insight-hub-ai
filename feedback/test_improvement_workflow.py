import smtplib
from unittest.mock import patch

from django.db import connection
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from accounts.models import User

from .models import (
    FeedbackSubmission,
    ImprovementDispatch,
    ImprovementNotice,
    ImprovementStatusHistory,
    ImprovementUpdate,
    Survey,
)
from .improvement_workflow import (
    ImprovementTransitionError,
    record_initial_status,
    transition_improvement,
)
from .notice_service import resolve_notice_recipients
from .notice_service import prepare_notice_dispatches, send_notice_batch
from .local_service import get_customer_notifications_payload


class ImprovementLifecycleModelTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(
            username="workflow-manager",
            password="test-password",
            role=User.Role.MANAGER,
        )
        self.survey = Survey.objects.create(
            title="改善流程問卷",
            slug="improvement-workflow-survey",
        )

    def test_new_improvement_defaults_to_draft_without_notification_intent(self):
        improvement = ImprovementUpdate.objects.create(
            survey=self.survey,
            title="改善項目",
            summary="只建立內部工作項目。",
        )

        self.assertEqual(improvement.status, ImprovementUpdate.Status.DRAFT)
        self.assertEqual(improvement.priority, ImprovementUpdate.Priority.MEDIUM)
        self.assertFalse(improvement.send_global_notice)
        self.assertIsNone(improvement.completed_at)
        self.assertIsNone(improvement.archived_at)

    def test_status_history_keeps_actor_and_transition(self):
        improvement = ImprovementUpdate.objects.create(
            survey=self.survey,
            title="改善項目",
            summary="測試狀態稽核。",
        )
        history = ImprovementStatusHistory.objects.create(
            improvement=improvement,
            from_status=ImprovementUpdate.Status.DRAFT,
            to_status=ImprovementUpdate.Status.PLANNED,
            changed_by=self.manager,
        )

        self.assertEqual(history.changed_by, self.manager)
        self.assertEqual(list(improvement.status_history.all()), [history])

    @patch("feedback.views.send_mail")
    def test_create_improvement_never_dispatches_or_sends_email(self, send_mail):
        customer = User.objects.create_user(
            username="workflow-customer",
            password="test-password",
            email="workflow-customer@example.com",
            notification_opt_in=True,
        )
        FeedbackSubmission.objects.create(
            survey=self.survey,
            user=customer,
            respondent_email=customer.email,
            consent_follow_up=True,
        )
        self.client.force_login(self.manager)

        response = self.client.post(
            reverse("feedback:improvement-create", args=[self.survey.slug]),
            {
                "title": "改善項目",
                "summary": "儲存與通知必須分離。",
                "related_category": "流程",
                "send_global_notice": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        improvement = ImprovementUpdate.objects.get()
        self.assertFalse(improvement.send_global_notice)
        self.assertEqual(improvement.created_by, self.manager)
        self.assertTrue(
            improvement.status_history.filter(
                from_status="",
                to_status=ImprovementUpdate.Status.DRAFT,
                changed_by=self.manager,
            ).exists()
        )
        self.assertEqual(ImprovementDispatch.objects.count(), 0)
        send_mail.assert_not_called()


class ImprovementDetailAndStatusTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(
            username="detail-manager",
            password="test-password",
            role=User.Role.MANAGER,
        )
        self.customer = User.objects.create_user(
            username="detail-customer",
            password="test-password",
            role=User.Role.CUSTOMER,
        )
        self.survey = Survey.objects.create(
            title="詳細頁問卷",
            slug="improvement-detail-survey",
        )
        self.improvement = ImprovementUpdate.objects.create(
            survey=self.survey,
            title="原始改善標題",
            summary="原始改善摘要",
            related_category="流程",
            source_ai_draft_id="audit-draft-id",
            source_evidence_refs=["evidence-1"],
            source_ai_metadata={"priority": "high", "rationale": "來源稽核"},
            created_by=self.manager,
            updated_by=self.manager,
        )
        record_initial_status(self.improvement, self.manager)

    def test_manager_can_view_detail_and_customer_cannot(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("feedback:improvement-detail", args=[self.improvement.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "原始改善標題")
        self.assertContains(response, "草稿")

        self.client.force_login(self.customer)
        response = self.client.get(reverse("feedback:improvement-detail", args=[self.improvement.pk]))
        self.assertEqual(response.status_code, 403)

    @patch("feedback.views.send_mail")
    def test_edit_changes_only_editable_fields_without_notification(self, send_mail):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("feedback:improvement-update", args=[self.improvement.pk]),
            {
                "title": "更新後標題",
                "summary": "更新後摘要",
                "related_category": "服務",
                "priority": ImprovementUpdate.Priority.HIGH,
                "due_date": "2026-08-30",
                "internal_note": "僅供管理者查看",
                "survey": "999999",
                "status": ImprovementUpdate.Status.COMPLETED,
                "source_ai_draft_id": "tampered",
                "source_evidence_refs": "[]",
                "send_global_notice": "on",
            },
        )

        self.assertRedirects(
            response,
            reverse("feedback:improvement-detail", args=[self.improvement.pk]),
        )
        self.improvement.refresh_from_db()
        self.assertEqual(self.improvement.title, "更新後標題")
        self.assertEqual(self.improvement.summary, "更新後摘要")
        self.assertEqual(self.improvement.priority, ImprovementUpdate.Priority.HIGH)
        self.assertEqual(self.improvement.survey, self.survey)
        self.assertEqual(self.improvement.status, ImprovementUpdate.Status.DRAFT)
        self.assertEqual(self.improvement.source_ai_draft_id, "audit-draft-id")
        self.assertEqual(self.improvement.source_evidence_refs, ["evidence-1"])
        self.assertEqual(self.improvement.source_ai_metadata["rationale"], "來源稽核")
        self.assertFalse(self.improvement.send_global_notice)
        self.assertEqual(self.improvement.updated_by, self.manager)
        self.assertEqual(ImprovementDispatch.objects.count(), 0)
        send_mail.assert_not_called()

    def test_status_flow_tracks_completion_reopen_archive_and_restore(self):
        transition_improvement(self.improvement.pk, ImprovementUpdate.Status.PLANNED, self.manager)
        transition_improvement(self.improvement.pk, ImprovementUpdate.Status.IN_PROGRESS, self.manager)
        transition_improvement(self.improvement.pk, ImprovementUpdate.Status.COMPLETED, self.manager)
        self.improvement.refresh_from_db()
        self.assertIsNotNone(self.improvement.completed_at)

        transition_improvement(self.improvement.pk, ImprovementUpdate.Status.IN_PROGRESS, self.manager)
        self.improvement.refresh_from_db()
        self.assertIsNone(self.improvement.completed_at)

        transition_improvement(self.improvement.pk, ImprovementUpdate.Status.ARCHIVED, self.manager)
        self.improvement.refresh_from_db()
        self.assertIsNotNone(self.improvement.archived_at)
        self.assertEqual(self.improvement.status, ImprovementUpdate.Status.ARCHIVED)

        transition_improvement(self.improvement.pk, "restore", self.manager)
        self.improvement.refresh_from_db()
        self.assertEqual(self.improvement.status, ImprovementUpdate.Status.IN_PROGRESS)
        self.assertIsNone(self.improvement.archived_at)
        self.assertEqual(self.improvement.status_history.count(), 7)

    def test_archiving_completed_item_preserves_completion_timestamp(self):
        transition_improvement(self.improvement.pk, ImprovementUpdate.Status.PLANNED, self.manager)
        transition_improvement(self.improvement.pk, ImprovementUpdate.Status.IN_PROGRESS, self.manager)
        transition_improvement(self.improvement.pk, ImprovementUpdate.Status.COMPLETED, self.manager)
        self.improvement.refresh_from_db()
        completed_at = self.improvement.completed_at

        transition_improvement(self.improvement.pk, ImprovementUpdate.Status.ARCHIVED, self.manager)
        transition_improvement(self.improvement.pk, "restore", self.manager)

        self.improvement.refresh_from_db()
        self.assertEqual(self.improvement.status, ImprovementUpdate.Status.COMPLETED)
        self.assertEqual(self.improvement.completed_at, completed_at)

    def test_invalid_status_jump_is_rejected_without_history(self):
        history_count = self.improvement.status_history.count()
        with self.assertRaises(ImprovementTransitionError):
            transition_improvement(
                self.improvement.pk,
                ImprovementUpdate.Status.COMPLETED,
                self.manager,
            )
        self.improvement.refresh_from_db()
        self.assertEqual(self.improvement.status, ImprovementUpdate.Status.DRAFT)
        self.assertEqual(self.improvement.status_history.count(), history_count)

    def test_archived_item_cannot_be_edited_until_restored(self):
        transition_improvement(self.improvement.pk, ImprovementUpdate.Status.ARCHIVED, self.manager)
        self.client.force_login(self.manager)
        response = self.client.get(reverse("feedback:improvement-update", args=[self.improvement.pk]))
        self.assertRedirects(
            response,
            reverse("feedback:improvement-detail", args=[self.improvement.pk]),
        )

    def test_update_and_status_post_require_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.manager)

        update_response = csrf_client.post(
            reverse("feedback:improvement-update", args=[self.improvement.pk]),
            {
                "title": "不可更新",
                "summary": "缺少 CSRF",
                "related_category": "",
                "priority": ImprovementUpdate.Priority.MEDIUM,
                "due_date": "",
                "internal_note": "",
            },
        )
        status_response = csrf_client.post(
            reverse("feedback:improvement-status", args=[self.improvement.pk]),
            {"status": ImprovementUpdate.Status.PLANNED},
        )

        self.assertEqual(update_response.status_code, 403)
        self.assertEqual(status_response.status_code, 403)


class ImprovementNoticeDraftTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(
            username="notice-manager",
            password="test-password",
            role=User.Role.MANAGER,
            email="notice-manager@example.com",
            is_email_verified=True,
        )
        self.survey = Survey.objects.create(
            title="通知流程問卷",
            slug="notice-workflow-survey",
        )
        self.improvement = ImprovementUpdate.objects.create(
            survey=self.survey,
            title="改善等候流程",
            summary="已重新安排尖峰時段人力。",
            created_by=self.manager,
            updated_by=self.manager,
        )
        record_initial_status(self.improvement, self.manager)

    def _customer(self, username, email, **kwargs):
        defaults = {
            "role": User.Role.CUSTOMER,
            "is_active": True,
            "is_email_verified": True,
            "notification_opt_in": True,
        }
        defaults.update(kwargs)
        return User.objects.create_user(
            username=username,
            password="test-password",
            email=email,
            **defaults,
        )

    def test_global_recipient_resolution_is_private_valid_and_deduplicated(self):
        selected = self._customer("selected", "same@example.com")
        self._customer("duplicate", "SAME@example.com")
        self._customer("opted-out", "out@example.com", notification_opt_in=False)
        self._customer("unverified", "unverified@example.com", is_email_verified=False)
        self._customer("invalid", "invalid-email")
        notice = ImprovementNotice.objects.create(
            improvement=self.improvement,
            subject="全域通知",
            body="內容",
            audience_type=ImprovementNotice.AudienceType.GLOBAL,
            created_by=self.manager,
        )

        recipients = resolve_notice_recipients(notice)

        self.assertEqual(len(recipients), 1)
        self.assertEqual(recipients[0].user_id, selected.id)
        self.assertNotIn("same@example.com", repr(recipients[0]))

    def test_survey_recipient_resolution_requires_consent_and_linked_user_preferences(self):
        selected = self._customer("survey-selected", "selected@example.com")
        opted_out = self._customer(
            "survey-opted-out",
            "out@example.com",
            notification_opt_in=False,
        )
        no_consent = self._customer("survey-no-consent", "no-consent@example.com")
        FeedbackSubmission.objects.create(
            survey=self.survey,
            user=selected,
            respondent_email=selected.email,
            consent_follow_up=True,
        )
        FeedbackSubmission.objects.create(
            survey=self.survey,
            user=selected,
            respondent_email=selected.email,
            consent_follow_up=True,
        )
        FeedbackSubmission.objects.create(
            survey=self.survey,
            user=opted_out,
            respondent_email=opted_out.email,
            consent_follow_up=True,
        )
        FeedbackSubmission.objects.create(
            survey=self.survey,
            user=no_consent,
            respondent_email=no_consent.email,
            consent_follow_up=False,
        )
        anonymous = FeedbackSubmission.objects.create(
            survey=self.survey,
            respondent_email="anonymous@example.com",
            consent_follow_up=True,
        )
        notice = ImprovementNotice.objects.create(
            improvement=self.improvement,
            subject="問卷通知",
            body="內容",
            audience_type=ImprovementNotice.AudienceType.SURVEY_RESPONDENTS,
            created_by=self.manager,
        )

        recipients = resolve_notice_recipients(notice)

        self.assertEqual(len(recipients), 2)
        self.assertEqual({item.user_id for item in recipients}, {selected.id, None})
        self.assertIn(anonymous.id, {item.submission_id for item in recipients})

    @patch("feedback.views.send_mail")
    def test_create_and_edit_notice_stays_draft_without_dispatch_or_email(self, send_mail):
        self.client.force_login(self.manager)
        create_response = self.client.post(
            reverse("feedback:notice-batch-create", args=[self.improvement.pk]),
            {
                "subject": "第一版通知",
                "body": "第一版內容",
                "audience_type": ImprovementNotice.AudienceType.SURVEY_RESPONDENTS,
            },
        )
        notice = ImprovementNotice.objects.get()
        self.assertRedirects(
            create_response,
            reverse("feedback:notice-batch-preview", args=[notice.pk]),
        )
        self.assertEqual(notice.status, ImprovementNotice.Status.DRAFT)
        self.assertEqual(notice.created_by, self.manager)
        old_token = notice.confirmation_token

        edit_response = self.client.post(
            reverse("feedback:notice-batch-update", args=[notice.pk]),
            {
                "subject": "第二版通知",
                "body": "第二版內容",
                "audience_type": ImprovementNotice.AudienceType.GLOBAL,
            },
        )
        self.assertRedirects(
            edit_response,
            reverse("feedback:notice-batch-preview", args=[notice.pk]),
        )
        notice.refresh_from_db()
        self.assertEqual(notice.subject, "第二版通知")
        self.assertEqual(notice.content_version, 2)
        self.assertNotEqual(notice.confirmation_token, old_token)
        self.assertEqual(ImprovementDispatch.objects.count(), 0)
        send_mail.assert_not_called()

    def test_preview_shows_count_without_email_addresses(self):
        selected = self._customer("preview-selected", "private@example.com")
        FeedbackSubmission.objects.create(
            survey=self.survey,
            user=selected,
            respondent_email=selected.email,
            consent_follow_up=True,
        )
        notice = ImprovementNotice.objects.create(
            improvement=self.improvement,
            subject="預覽通知",
            body="預覽內容",
            audience_type=ImprovementNotice.AudienceType.SURVEY_RESPONDENTS,
            created_by=self.manager,
        )
        self.client.force_login(self.manager)

        response = self.client.get(reverse("feedback:notice-batch-preview", args=[notice.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1 人")
        self.assertNotContains(response, "private@example.com")

    def test_notice_draft_views_require_manager_and_csrf(self):
        customer = self._customer("notice-customer", "customer@example.com")
        self.client.force_login(customer)
        response = self.client.get(reverse("feedback:notice-batch-create", args=[self.improvement.pk]))
        self.assertEqual(response.status_code, 403)

        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.manager)
        response = csrf_client.post(
            reverse("feedback:notice-batch-create", args=[self.improvement.pk]),
            {
                "subject": "缺少 CSRF",
                "body": "不應建立",
                "audience_type": ImprovementNotice.AudienceType.GLOBAL,
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ImprovementNotice.objects.exists())


class ImprovementNoticeDeliveryTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(
            username="delivery-manager",
            password="test-password",
            role=User.Role.MANAGER,
        )
        self.survey = Survey.objects.create(
            title="寄送測試問卷",
            slug="notice-delivery-survey",
        )
        self.improvement = ImprovementUpdate.objects.create(
            survey=self.survey,
            title="原始改善內容",
            summary="原始改善摘要",
            created_by=self.manager,
            updated_by=self.manager,
        )

    def _customer_submission(self, suffix):
        user = User.objects.create_user(
            username=f"delivery-{suffix}",
            password="test-password",
            role=User.Role.CUSTOMER,
            email=f"delivery-{suffix}@example.com",
            is_email_verified=True,
            notification_opt_in=True,
        )
        submission = FeedbackSubmission.objects.create(
            survey=self.survey,
            user=user,
            respondent_email=user.email,
            consent_follow_up=True,
        )
        return user, submission

    def _notice(self):
        return ImprovementNotice.objects.create(
            improvement=self.improvement,
            subject="已凍結通知主旨",
            body="已凍結通知內容",
            audience_type=ImprovementNotice.AudienceType.SURVEY_RESPONDENTS,
            created_by=self.manager,
        )

    def _send_payload(self, notice):
        return {
            "confirmation_token": str(notice.confirmation_token),
            "content_version": str(notice.content_version),
        }

    @patch("feedback.notice_service.send_mail", return_value=1)
    def test_confirm_send_is_idempotent_and_freezes_content(self, send_mail):
        customer, _ = self._customer_submission("success")
        notice = self._notice()
        payload = self._send_payload(notice)
        self.client.force_login(self.manager)

        first_response = self.client.post(
            reverse("feedback:notice-batch-send", args=[notice.pk]),
            payload,
        )
        second_response = self.client.post(
            reverse("feedback:notice-batch-send", args=[notice.pk]),
            payload,
        )

        self.assertRedirects(
            first_response,
            reverse("feedback:notice-batch-detail", args=[notice.pk]),
        )
        self.assertRedirects(
            second_response,
            reverse("feedback:notice-batch-detail", args=[notice.pk]),
        )
        notice.refresh_from_db()
        dispatch = ImprovementDispatch.objects.get(notice=notice)
        self.assertEqual(notice.status, ImprovementNotice.Status.SENT)
        self.assertEqual(notice.recipient_count, 1)
        self.assertEqual(notice.sent_count, 1)
        self.assertEqual(dispatch.delivery_status, ImprovementDispatch.DeliveryStatus.SENT)
        self.assertEqual(dispatch.attempt_count, 1)
        self.assertEqual(send_mail.call_count, 1)
        self.assertEqual(send_mail.call_args.kwargs["subject"], "已凍結通知主旨")
        self.assertEqual(send_mail.call_args.kwargs["message"], "已凍結通知內容")

        self.improvement.title = "後續修改的改善標題"
        self.improvement.summary = "後續修改的改善摘要"
        self.improvement.save(update_fields=["title", "summary", "updated_at"])
        customer_payload = get_customer_notifications_payload(customer)
        self.assertEqual(customer_payload["notices"][0]["improvement"]["title"], "已凍結通知主旨")
        self.assertEqual(customer_payload["notices"][0]["improvement"]["summary"], "已凍結通知內容")

    @patch("feedback.notice_service.send_mail")
    def test_stale_preview_and_empty_audience_do_not_send(self, send_mail):
        notice = self._notice()
        self.client.force_login(self.manager)

        stale_response = self.client.post(
            reverse("feedback:notice-batch-send", args=[notice.pk]),
            {
                "confirmation_token": str(notice.confirmation_token),
                "content_version": notice.content_version + 1,
            },
        )
        empty_response = self.client.post(
            reverse("feedback:notice-batch-send", args=[notice.pk]),
            self._send_payload(notice),
        )

        self.assertRedirects(
            stale_response,
            reverse("feedback:notice-batch-preview", args=[notice.pk]),
        )
        self.assertRedirects(
            empty_response,
            reverse("feedback:notice-batch-preview", args=[notice.pk]),
        )
        notice.refresh_from_db()
        self.assertEqual(notice.status, ImprovementNotice.Status.DRAFT)
        self.assertFalse(ImprovementDispatch.objects.exists())
        send_mail.assert_not_called()

    @patch(
        "feedback.notice_service.send_mail",
        side_effect=[1, smtplib.SMTPServerDisconnected("provider unavailable")],
    )
    def test_partial_failure_records_safe_error_and_retry_only_failed(self, first_send):
        self._customer_submission("partial-a")
        self._customer_submission("partial-b")
        notice = self._notice()
        self.client.force_login(self.manager)

        self.client.post(
            reverse("feedback:notice-batch-send", args=[notice.pk]),
            self._send_payload(notice),
        )

        notice.refresh_from_db()
        self.assertEqual(notice.status, ImprovementNotice.Status.PARTIALLY_SENT)
        self.assertEqual(notice.sent_count, 1)
        self.assertEqual(notice.failed_count, 1)
        failed = notice.dispatches.get(delivery_status=ImprovementDispatch.DeliveryStatus.FAILED)
        sent = notice.dispatches.get(delivery_status=ImprovementDispatch.DeliveryStatus.SENT)
        self.assertEqual(failed.error_code, "provider_unavailable")
        self.assertNotIn("provider unavailable", failed.error_code)

        with patch("feedback.notice_service.send_mail", return_value=1) as retry_send:
            first_retry = self.client.post(reverse("feedback:notice-batch-retry", args=[notice.pk]))
            second_retry = self.client.post(reverse("feedback:notice-batch-retry", args=[notice.pk]))

        self.assertRedirects(first_retry, reverse("feedback:notice-batch-detail", args=[notice.pk]))
        self.assertRedirects(second_retry, reverse("feedback:notice-batch-detail", args=[notice.pk]))
        notice.refresh_from_db()
        failed.refresh_from_db()
        sent.refresh_from_db()
        self.assertEqual(notice.status, ImprovementNotice.Status.SENT)
        self.assertEqual(notice.sent_count, 2)
        self.assertEqual(notice.failed_count, 0)
        self.assertEqual(failed.attempt_count, 2)
        self.assertEqual(sent.attempt_count, 1)
        self.assertEqual(retry_send.call_count, 1)
        self.assertEqual(first_send.call_count, 2)

    @patch("feedback.notice_service.send_mail", return_value=1)
    def test_global_dispatch_can_be_read_by_recipient_user(self, send_mail):
        customer = User.objects.create_user(
            username="global-recipient",
            password="test-password",
            role=User.Role.CUSTOMER,
            email="global-recipient@example.com",
            is_email_verified=True,
            notification_opt_in=True,
        )
        notice = self._notice()
        notice.audience_type = ImprovementNotice.AudienceType.GLOBAL
        notice.save(update_fields=["audience_type", "updated_at"])
        self.client.force_login(self.manager)
        self.client.post(
            reverse("feedback:notice-batch-send", args=[notice.pk]),
            self._send_payload(notice),
        )
        dispatch = notice.dispatches.get()
        self.assertIsNone(dispatch.submission_id)
        self.assertEqual(dispatch.recipient_user, customer)

        self.client.force_login(customer)
        response = self.client.post(
            reverse("feedback:notice-mark-read", args=[dispatch.pk]),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        dispatch.refresh_from_db()
        self.assertTrue(dispatch.is_read)
        send_mail.assert_called_once()

    def test_send_and_retry_post_require_csrf(self):
        self._customer_submission("csrf")
        notice = self._notice()
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.manager)

        send_response = csrf_client.post(
            reverse("feedback:notice-batch-send", args=[notice.pk]),
            self._send_payload(notice),
        )
        retry_response = csrf_client.post(reverse("feedback:notice-batch-retry", args=[notice.pk]))

        self.assertEqual(send_response.status_code, 403)
        self.assertEqual(retry_response.status_code, 403)
        notice.refresh_from_db()
        self.assertEqual(notice.status, ImprovementNotice.Status.DRAFT)

    @patch("feedback.notice_service.send_mail")
    def test_recipient_is_rechecked_after_confirmation(self, send_mail):
        customer, submission = self._customer_submission("changed-consent")
        notice = self._notice()
        prepare_notice_dispatches(
            notice.pk,
            confirmation_token=notice.confirmation_token,
            content_version=notice.content_version,
            actor=self.manager,
        )
        submission.consent_follow_up = False
        submission.save(update_fields=["consent_follow_up"])

        result = send_notice_batch(notice.pk)

        dispatch = result.dispatches.get()
        self.assertEqual(result.status, ImprovementNotice.Status.FAILED)
        self.assertEqual(dispatch.error_code, "recipient_ineligible")
        self.assertEqual(dispatch.recipient_user, customer)
        send_mail.assert_not_called()


class ImprovementNoticeTransactionBoundaryTests(TransactionTestCase):
    reset_sequences = True

    @patch("feedback.notice_service.send_mail")
    def test_email_is_sent_after_confirmation_transaction_commits(self, send_mail):
        manager = User.objects.create_user(
            username="transaction-manager",
            password="test-password",
            role=User.Role.MANAGER,
        )
        customer = User.objects.create_user(
            username="transaction-customer",
            password="test-password",
            role=User.Role.CUSTOMER,
            email="transaction-customer@example.com",
            is_email_verified=True,
            notification_opt_in=True,
        )
        survey = Survey.objects.create(title="交易問卷", slug="transaction-survey")
        FeedbackSubmission.objects.create(
            survey=survey,
            user=customer,
            respondent_email=customer.email,
            consent_follow_up=True,
        )
        improvement = ImprovementUpdate.objects.create(
            survey=survey,
            title="交易改善",
            summary="確認 commit 邊界。",
        )
        notice = ImprovementNotice.objects.create(
            improvement=improvement,
            subject="交易通知",
            body="交易通知內容",
            audience_type=ImprovementNotice.AudienceType.SURVEY_RESPONDENTS,
            created_by=manager,
        )
        atomic_states = []

        def observe_transaction(**kwargs):
            atomic_states.append(connection.in_atomic_block)
            return 1

        send_mail.side_effect = observe_transaction
        self.client.force_login(manager)

        self.client.post(
            reverse("feedback:notice-batch-send", args=[notice.pk]),
            {
                "confirmation_token": str(notice.confirmation_token),
                "content_version": notice.content_version,
            },
        )

        self.assertEqual(atomic_states, [False])
