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


@cloud_only
class CustomerTimelineTests(TestCase):
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
