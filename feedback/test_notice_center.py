from datetime import timedelta
import uuid

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from cloudapi.models import NodeDevice, SubmissionReceipt
from cloudapi.writes import assign_survey_to_node
from feedback.models import ImprovementDispatch, ImprovementNotice, ImprovementUpdate, Survey
from feedback.notice_service import NOTICE_STAGES, notice_center_groups
from feedback.test_utils import cloud_only


class NoticeCenterBase(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(username="nm", password="x", role=User.Role.MANAGER)
        self.survey = Survey.objects.create(title="S", slug="s")
        self.improvement = ImprovementUpdate.objects.create(
            survey=self.survey, title="t", summary="s", created_by=self.manager, updated_by=self.manager)

    def notice(self, status="draft", improvement=None):
        return ImprovementNotice.objects.create(
            improvement=improvement or self.improvement, subject="s", body="b", status=status,
            audience_type=ImprovementNotice.AudienceType.SURVEY_RESPONDENTS, created_by=self.manager)

    def dispatch(self, notice, *, status="sent", is_read=False):
        user = User.objects.create_user(
            username=f"u{ImprovementDispatch.objects.count()}", password="x", role=User.Role.CUSTOMER)
        return ImprovementDispatch.objects.create(
            improvement=notice.improvement, notice=notice, recipient_user=user,
            recipient_key=uuid.uuid4().hex, delivery_status=status, is_read=is_read)


class NoticeStageTests(NoticeCenterBase):
    def test_stage_definition(self):
        self.assertEqual([key for key, _, _ in NOTICE_STAGES], ["draft", "sending", "done", "attention"])

    def test_stage_counts(self):
        for status in ("draft", "sending", "sent", "partially_sent", "failed"):
            self.notice(status)
        (group,) = notice_center_groups(self.survey)
        counts = {stage["key"]: stage["count"] for stage in group["stages"]}
        self.assertEqual(counts, {"draft": 1, "sending": 1, "done": 1, "attention": 2})
        self.assertEqual(len(group["batches"]), 5)

    def test_groups_split_by_improvement_and_skip_noticeless(self):
        other = ImprovementUpdate.objects.create(
            survey=self.survey, title="o", summary="s", created_by=self.manager, updated_by=self.manager)
        ImprovementUpdate.objects.create(
            survey=self.survey, title="none", summary="s", created_by=self.manager, updated_by=self.manager)
        self.notice("draft")
        self.notice("sent", improvement=other)
        groups = notice_center_groups(self.survey)
        self.assertEqual({g["improvement"].pk for g in groups}, {self.improvement.pk, other.pk})
        self.assertEqual(notice_center_groups(None), [])

    def test_site_read_rate(self):
        notice = self.notice("sent")
        self.dispatch(notice, is_read=True)
        self.dispatch(notice)
        self.dispatch(notice)
        self.dispatch(notice, status="failed", is_read=True)
        (group,) = notice_center_groups(self.survey)
        self.assertEqual(group["batches"][0]["site_read_rate"], 33)

    def test_site_read_rate_none_when_nothing_sent(self):
        notice = self.notice("sending")
        self.dispatch(notice, status="pending")
        (group,) = notice_center_groups(self.survey)
        self.assertIsNone(group["batches"][0]["site_read_rate"])


class NoticeCenterViewTests(NoticeCenterBase):
    def test_context_has_groups_and_legacy_names(self):
        self.notice("draft")
        self.client.force_login(self.manager)
        response = self.client.get(reverse("feedback:notice-center"), {"survey": self.survey.slug})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["notice_groups"]), 1)
        self.assertEqual(response.context["notices"].count(), 1)
        self.assertEqual(response.context["legacy_notices"].count(), 0)

    def test_totals_sum_deliveries_and_flag_batches_needing_attention(self):
        sent = self.notice("sent")
        ImprovementNotice.objects.filter(pk=sent.pk).update(sent_count=5)
        partial = self.notice("partially_sent")
        ImprovementNotice.objects.filter(pk=partial.pk).update(sent_count=2, failed_count=1)
        self.notice("draft")
        self.client.force_login(self.manager)
        response = self.client.get(reverse("feedback:notice-center"), {"survey": self.survey.slug})
        self.assertEqual(response.context["notice_totals"],
                         {"batches": 3, "delivered": 7, "drafts": 1, "attention": 1})
        self.assertContains(response, "失敗 1")


@cloud_only
class NodeSurveyResponseCountTests(NoticeCenterBase):
    def test_node_survey_response_count_uses_receipts(self):
        node, _ = NodeDevice.issue("office")
        survey = assign_survey_to_node(Survey.objects.create(title="N", slug="n"), node).survey
        customer = User.objects.create_user(username="nc", password="x", role=User.Role.CUSTOMER)
        for sequence in (1, 2):
            SubmissionReceipt.objects.create(
                submission_uuid=uuid.uuid4(), node=node, survey=survey, submitted_at=timezone.now(),
                definition_version=1, response_sequence=sequence, payload_hash="0" * 64,
                status="received", user=customer, consent_follow_up=True)
        self.client.force_login(self.manager)
        response = self.client.get(reverse("feedback:notice-center"))
        rows = {row.pk: row for row in response.context["notice_survey_rows"]}
        self.assertEqual(rows[survey.pk].response_count, 2)


@cloud_only
class NodeSurveyLatestReplyTests(NoticeCenterBase):
    def _node_survey_with_receipts(self):
        node, _ = NodeDevice.issue("office")
        survey = assign_survey_to_node(Survey.objects.create(title="N", slug="n"), node).survey
        customer = User.objects.create_user(username="nc2", password="x", role=User.Role.CUSTOMER)
        times = [timezone.now() - timedelta(days=2), timezone.now() - timedelta(days=1)]
        for sequence, submitted_at in enumerate(times, start=1):
            SubmissionReceipt.objects.create(
                submission_uuid=uuid.uuid4(), node=node, survey=survey, submitted_at=submitted_at,
                definition_version=1, response_sequence=sequence, payload_hash="0" * 64,
                status="received", user=customer, consent_follow_up=True)
        SubmissionReceipt.objects.create(
            submission_uuid=uuid.uuid4(), node=node, survey=survey, submitted_at=timezone.now(),
            definition_version=1, response_sequence=3, payload_hash="0" * 64,
            status="abandoned", user=customer, consent_follow_up=True)
        return survey, times[-1]

    def test_notice_center_latest_reply_uses_receipts(self):
        survey, newest = self._node_survey_with_receipts()
        self.client.force_login(self.manager)
        response = self.client.get(reverse("feedback:notice-center"))
        rows = {row.pk: row for row in response.context["notice_survey_rows"]}
        self.assertEqual(rows[survey.pk].latest_submission_at, newest)
        self.assertEqual(rows[survey.pk].response_count, 2)

    def test_improvement_list_counts_and_latest_use_receipts(self):
        survey, newest = self._node_survey_with_receipts()
        self.client.force_login(self.manager)
        response = self.client.get(reverse("feedback:improvement-list"))
        rows = {row.pk: row for row in response.context["improvement_survey_rows"]}
        self.assertEqual(rows[survey.pk].response_count, 2)
        self.assertEqual(rows[survey.pk].latest_submission_at, newest)
