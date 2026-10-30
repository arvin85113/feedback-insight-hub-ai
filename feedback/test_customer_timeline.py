import uuid
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from cloudapi.models import NodeDevice, SubmissionReceipt
from cloudapi.writes import assign_survey_to_node
from feedback.local_service import get_customer_home_payload, get_customer_notifications_payload
from feedback.models import FeedbackSubmission, ImprovementDispatch, ImprovementNotice, ImprovementUpdate, Survey
from feedback.test_utils import cloud_only


class TimelineFixtures:
    """Shared fixtures and helpers (no tests)."""

    def setUp(self):
        self.manager = User.objects.create_user(username="tm", password="x", role=User.Role.MANAGER)
        self.node, _ = NodeDevice.issue("office")
        self.survey = assign_survey_to_node(Survey.objects.create(title="A", slug="a"), self.node).survey
        self.other_survey = assign_survey_to_node(Survey.objects.create(title="B", slug="b"), self.node).survey
        self.customer = User.objects.create_user(
            username="tc", password="x", role=User.Role.CUSTOMER, email="tc@example.com",
            is_email_verified=True)
        self.sequence = 0

    def receipt(self, survey=None, **overrides):
        self.sequence += 1
        fields = dict(
            submission_uuid=uuid.uuid4(), node=self.node, survey=survey or self.survey,
            submitted_at=timezone.now(), definition_version=1, response_sequence=self.sequence,
            payload_hash="0" * 64, status="received", user=self.customer, consent_follow_up=True)
        fields.update(overrides)
        return SubmissionReceipt.objects.create(**fields)

    def improvement(self, survey=None, **overrides):
        fields = dict(
            survey=survey or self.survey, title="改善標題", summary="改善摘要", status="in_progress",
            internal_note="SECRET-INTERNAL", created_by=self.manager, updated_by=self.manager)
        fields.update(overrides)
        return ImprovementUpdate.objects.create(**fields)

    def dispatch(self, improvement, audience="survey_respondents", user=None, status="sent",
                 subject="通知主旨", sent_at=None, is_read=False):
        notice = ImprovementNotice.objects.create(
            improvement=improvement, subject=subject, body="通知內文", audience_type=audience,
            created_by=self.manager)
        return ImprovementDispatch.objects.create(
            improvement=improvement, notice=notice, recipient_user=user or self.customer,
            recipient_key=f"k{notice.pk}", delivery_status=status,
            sent_at=sent_at or timezone.now(), is_read=is_read)


@cloud_only
class CustomerTimelineTests(TimelineFixtures, TestCase):
    def test_receipt_card_shows_sent_notice(self):
        self.receipt()
        self.dispatch(self.improvement())
        cards = get_customer_home_payload(self.customer)["survey_cards"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["state"], "notified")
        self.assertEqual(cards[0]["survey"]["slug"], "a")
        event = cards[0]["events"][0]
        self.assertEqual(event["title"], "通知主旨")
        self.assertEqual(event["summary"], "通知內文")
        self.assertEqual(event["improvement_status_label"], "進行中")
        self.assertFalse(event["is_read"])

    def test_global_notice_goes_to_announcements(self):
        self.receipt()
        self.dispatch(self.improvement(survey=self.other_survey), audience="global")
        payload = get_customer_home_payload(self.customer)
        self.assertEqual(len(payload["survey_cards"]), 1)
        self.assertEqual(payload["survey_cards"][0]["survey"]["slug"], "a")
        self.assertEqual(payload["survey_cards"][0]["events"], [])
        self.assertEqual(payload["survey_cards"][0]["state"], "waiting")
        self.assertEqual(len(payload["announcements"]), 1)
        self.assertEqual(payload["unread_count"], 1)

    def test_quarantined_receipt_is_processing(self):
        self.receipt(status="quarantined")
        self.assertEqual(get_customer_home_payload(self.customer)["survey_cards"][0]["state"], "processing")

    def test_submission_and_receipt_with_same_uuid_make_one_card(self):
        key = uuid.uuid4()
        FeedbackSubmission.objects.create(
            survey=self.survey, user=self.customer, respondent_email=self.customer.email,
            consent_follow_up=True, idempotency_key=key)
        self.receipt(submission_uuid=key)
        self.assertEqual(len(get_customer_home_payload(self.customer)["survey_cards"]), 1)

    def test_unsent_and_other_customers_notices_hidden(self):
        other = User.objects.create_user(username="other", password="x", role=User.Role.CUSTOMER)
        self.receipt()
        improvement = self.improvement()
        self.dispatch(improvement, status="pending")
        self.dispatch(improvement, user=other)
        payload = get_customer_home_payload(self.customer)
        self.assertEqual(payload["survey_cards"][0]["events"], [])
        self.assertEqual(payload["announcements"], [])
        self.assertEqual(payload["unread_count"], 0)

    def test_internal_fields_not_rendered(self):
        self.receipt()
        self.dispatch(self.improvement())
        self.client.force_login(self.customer)
        response = self.client.get(reverse("feedback:customer-home"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "SECRET-INTERNAL")

    def test_cards_newest_activity_first_and_events_oldest_first(self):
        now = timezone.now()
        self.receipt(submitted_at=now - timedelta(days=5))
        self.receipt(survey=self.other_survey, submitted_at=now - timedelta(days=4))
        improvement = self.improvement()
        self.dispatch(improvement, subject="new", sent_at=now - timedelta(days=1))
        self.dispatch(improvement, subject="old", sent_at=now - timedelta(days=3))
        cards = get_customer_home_payload(self.customer)["survey_cards"]
        self.assertEqual([c["survey"]["slug"] for c in cards], ["a", "b"])
        self.assertEqual([e["title"] for e in cards[0]["events"]], ["old", "new"])

    def test_notifications_grouped_unread_first(self):
        self.receipt()
        improvement = self.improvement()
        now = timezone.now()
        self.dispatch(improvement, subject="read", is_read=True, sent_at=now - timedelta(hours=2))
        self.dispatch(improvement, subject="unread", sent_at=now - timedelta(hours=3))
        self.dispatch(self.improvement(survey=self.other_survey), audience="global", subject="公告",
                      is_read=True, sent_at=now)
        payload = get_customer_notifications_payload(self.customer)
        self.assertEqual(len(payload["groups"]), 2)
        first = payload["groups"][0]
        self.assertEqual(first["survey"]["slug"], "a")
        self.assertIs(first["notices"][0]["is_read"], False)
        self.assertEqual(first["notices"][0]["title"], "unread")
        self.assertIsNone(payload["groups"][1]["survey"])
        self.assertEqual(payload["notice_count"], 3)
        self.assertEqual(payload["unread_count"], 1)

    def test_unanswered_survey_notice_not_on_home(self):
        self.receipt()
        self.dispatch(self.improvement(survey=self.other_survey))
        payload = get_customer_home_payload(self.customer)
        self.assertEqual(len(payload["survey_cards"]), 1)
        self.assertEqual(payload["survey_cards"][0]["events"], [])
        self.assertEqual(payload["announcements"], [])
        self.assertEqual(payload["unread_count"], 0)

    def test_notifications_history_groups_by_audience_not_membership(self):
        self.receipt()
        self.dispatch(self.improvement(survey=self.other_survey), subject="B-only")
        self.dispatch(self.improvement(survey=self.survey), audience="global", subject="公告")
        payload = get_customer_notifications_payload(self.customer)
        by_slug = {(g["survey"] or {}).get("slug"): g for g in payload["groups"]}
        self.assertEqual(set(by_slug), {"b", None})
        self.assertEqual(by_slug["b"]["notices"][0]["title"], "B-only")
        self.assertEqual(by_slug[None]["notices"][0]["title"], "公告")
        self.assertEqual(payload["notice_count"], 2)

    def test_multiple_replies_in_one_survey_make_one_card_with_all_events(self):
        FeedbackSubmission.objects.create(
            survey=self.survey, user=self.customer, respondent_email=self.customer.email,
            consent_follow_up=True, idempotency_key=uuid.uuid4())
        self.receipt()
        improvement = self.improvement()
        self.dispatch(improvement, subject="one")
        self.dispatch(improvement, subject="two")
        cards = get_customer_home_payload(self.customer)["survey_cards"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(sorted(e["title"] for e in cards[0]["events"]), ["one", "two"])
        self.assertEqual(cards[0]["state"], "notified")

    def test_two_receipts_in_one_survey_make_one_card(self):
        self.receipt()
        self.receipt()
        improvement = self.improvement()
        self.dispatch(improvement, subject="one")
        self.dispatch(improvement, subject="two")
        cards = get_customer_home_payload(self.customer)["survey_cards"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(len(cards[0]["events"]), 2)

    def test_quarantined_plus_received_receipt_is_waiting(self):
        self.receipt(status="quarantined")
        self.receipt(status="received")
        self.assertEqual(get_customer_home_payload(self.customer)["survey_cards"][0]["state"], "waiting")

    def test_submission_plus_quarantined_receipt_is_waiting(self):
        FeedbackSubmission.objects.create(
            survey=self.survey, user=self.customer, respondent_email=self.customer.email,
            consent_follow_up=True, idempotency_key=uuid.uuid4())
        self.receipt(status="quarantined")
        self.assertEqual(get_customer_home_payload(self.customer)["survey_cards"][0]["state"], "waiting")


@cloud_only
class CustomerTimelineTemplateTests(TimelineFixtures, TestCase):
    """Template rendering of the customer home and notification pages."""

    def get_page(self, name):
        self.client.force_login(self.customer)
        response = self.client.get(reverse(name))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_home_renders_timeline_and_waiting_copy(self):
        self.receipt()
        html = self.get_page("feedback:customer-home")
        self.assertIn("已填答", html)
        self.assertIn("等待店家回應", html)
        self.assertIn('class="cx-tl-item is-done"', html)
        self.assertIn('class="cx-tl-item is-waiting"', html)

    def test_home_renders_processing_copy(self):
        self.receipt(status="quarantined")
        html = self.get_page("feedback:customer-home")
        self.assertIn("店家處理中", html)

    def test_home_renders_events_and_hides_internal_fields(self):
        self.receipt()
        self.dispatch(self.improvement(), subject="已修正洗手間")
        html = self.get_page("feedback:customer-home")
        self.assertIn("已修正洗手間", html)
        self.assertIn("進行中", html)
        self.assertIn('class="cx-tl-item is-new"', html)
        self.assertIn('class="cx-tag cx-tag-new"', html)
        self.assertNotIn("SECRET-INTERNAL", html)
        self.assertNotIn("等待店家回應", html)

    def test_read_event_has_no_unread_pill(self):
        self.receipt()
        self.dispatch(self.improvement(), subject="已讀事件", is_read=True)
        html = self.get_page("feedback:customer-home")
        self.assertIn("已讀事件", html)
        self.assertNotIn("cx-tag-new", html)
        self.assertNotIn("is-new", html)

    def test_announcements_section_rendered(self):
        self.receipt()
        self.dispatch(self.improvement(survey=self.other_survey), audience="global", subject="全店公告")
        html = self.get_page("feedback:customer-home")
        self.assertIn("店家公告", html)
        self.assertIn("全店公告", html)

    def test_announcements_section_hidden_when_empty(self):
        self.receipt()
        self.assertNotIn("店家公告", self.get_page("feedback:customer-home"))

    def test_unread_notification_has_mark_read_form(self):
        self.receipt()
        dispatch = self.dispatch(self.improvement(), subject="分組通知")
        html = self.get_page("feedback:customer-notifications")
        self.assertIn('class="cx-list-item is-new"', html)
        self.assertIn(f'action="{reverse("feedback:notice-mark-read", args=[dispatch.pk])}"', html)
        self.assertIn("分組通知", html)
        self.assertIn(self.survey.title, html)

    def test_notifications_page_groups_announcements(self):
        self.receipt()
        self.dispatch(self.improvement(survey=self.other_survey), audience="global", subject="全店公告")
        html = self.get_page("feedback:customer-notifications")
        self.assertIn("店家公告", html)
        self.assertIn("全店公告", html)

    def test_announcement_row_has_mark_read_form(self):
        self.receipt()
        dispatch = self.dispatch(
            self.improvement(survey=self.other_survey), audience="global", subject="全店公告")
        html = self.get_page("feedback:customer-notifications")
        self.assertIn(f'action="{reverse("feedback:notice-mark-read", args=[dispatch.pk])}"', html)

    def test_read_notification_has_no_mark_read_form(self):
        self.receipt()
        self.dispatch(self.improvement(), subject="已讀通知", is_read=True)
        html = self.get_page("feedback:customer-notifications")
        self.assertIn("已讀通知", html)
        self.assertNotIn("data-mark-read", html)


@cloud_only
class CustomerTimelineReviewFixTests(TimelineFixtures, TestCase):
    def get_page(self, name):
        self.client.force_login(self.customer)
        response = self.client.get(reverse(name))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_internal_improvement_status_is_not_exposed(self):
        self.receipt()
        for status in ("draft", "archived"):
            self.dispatch(self.improvement(status=status), subject=f"內部-{status}")
        for payload in (
            get_customer_home_payload(self.customer)["survey_cards"][0]["events"],
            [n for g in get_customer_notifications_payload(self.customer)["groups"] for n in g["notices"]],
        ):
            self.assertEqual(len(payload), 2)
            for event in payload:
                self.assertIsNone(event["improvement_status_label"])
        for name in ("feedback:customer-home", "feedback:customer-notifications"):
            html = self.get_page(name)
            self.assertNotIn("草稿", html)
            self.assertNotIn("已封存", html)

    def test_customer_visible_statuses_keep_label(self):
        self.receipt()
        for status, label in (("planned", "已規劃"), ("in_progress", "進行中"), ("completed", "已完成")):
            self.dispatch(self.improvement(status=status))
            events = get_customer_home_payload(self.customer)["survey_cards"][0]["events"]
            self.assertIn(label, [e["improvement_status_label"] for e in events])

    def test_dates_use_local_timezone(self):
        from datetime import datetime, timezone as dt_timezone

        sent = datetime(2026, 10, 28, 17, 30, tzinfo=dt_timezone.utc)
        self.receipt(submitted_at=sent)
        self.dispatch(self.improvement(), sent_at=sent)
        self.dispatch(self.improvement(survey=self.other_survey), audience="global", sent_at=sent)
        card = get_customer_home_payload(self.customer)["survey_cards"][0]
        self.assertEqual(card["submitted_date"], "2026/10/29")
        self.assertEqual(card["events"][0]["sent_date"], "2026/10/29")
        for name in ("feedback:customer-home", "feedback:customer-notifications"):
            html = self.get_page(name)
            self.assertIn("2026/10/29", html)
            self.assertNotIn("2026-10-28", html)

    def test_non_global_dispatch_without_survey_is_grouped_and_counted(self):
        self.receipt()
        orphan = self.improvement()
        ImprovementUpdate.objects.filter(pk=orphan.pk).update(survey=None)
        dispatch = self.dispatch(orphan, audience="survey_respondents", subject="無問卷通知")
        payload = get_customer_notifications_payload(self.customer)
        self.assertEqual(payload["unread_count"], 1)
        self.assertEqual(payload["notice_count"], 1)
        self.assertIsNone(payload["groups"][0]["survey"])
        self.assertEqual(payload["groups"][0]["notices"][0]["id"], dispatch.id)

    def test_home_announcements_newest_first_and_capped(self):
        self.receipt()
        base = timezone.now() - timedelta(days=10)
        for index in range(7):
            self.dispatch(
                self.improvement(survey=self.other_survey), audience="global", subject=f"公告{index}",
                sent_at=base + timedelta(days=index))
        titles = [a["title"] for a in get_customer_home_payload(self.customer)["announcements"]]
        self.assertEqual(titles, ["公告6", "公告5", "公告4", "公告3", "公告2"])
