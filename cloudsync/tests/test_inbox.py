from datetime import datetime, timezone as dt_timezone

from django.test import TestCase

from cloudapi.envelope import envelope_payload_hash
from cloudsync.client import CLIENT, CloudError
from cloudsync.definitions import upsert_definition
from cloudsync.inbox import intake, sync_inbox
from cloudsync.models import PendingAck, SurveySyncState
from feedback.models import AnalysisJob, Answer, FeedbackSubmission, Survey

SURVEY_UUID = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"
Q1 = "b1b1b1b1-b1b1-b1b1-b1b1-b1b1b1b1b1b1"
Q2 = "b2b2b2b2-b2b2-b2b2-b2b2-b2b2b2b2b2b2"
Q3 = "b3b3b3b3-b3b3-b3b3-b3b3-b3b3b3b3b3b3"
U1 = "c1c1c1c1-c1c1-c1c1-c1c1-c1c1c1c1c1c1"
U2 = "c2c2c2c2-c2c2-c2c2-c2c2-c2c2c2c2c2c2"
U3 = "c3c3c3c3-c3c3-c3c3-c3c3-c3c3c3c3c3c3"
U9 = "c9c9c9c9-c9c9-c9c9-c9c9-c9c9c9c9c9c9"


def question(uuid_text, *, kind="short_text", order=1):
    choice = kind == "multiple_choice"
    return {"uuid": uuid_text, "code": "", "title": f"題目{order}", "help_text": "", "kind": kind,
            "data_type": "nominal" if choice else "text", "options_text": "甲\n乙" if choice else "",
            "is_required": False, "enable_keyword_tracking": False, "is_active": True, "order": order}


def definition(version, questions=None):
    return {"survey_uuid": SURVEY_UUID, "version": version, "title": "門市", "slug": "store", "description": "",
            "is_active": True, "analysis_enabled": True, "thank_you_email_enabled": True,
            "improvement_tracking_enabled": True, "category": None, "archived_at": None,
            "questions": questions if questions is not None else [question(Q1), question(Q2, kind="multiple_choice", order=2)]}


def envelope(sequence, submission_uuid, answers, version=1, consent=False):
    return {"submission_uuid": submission_uuid, "survey_uuid": SURVEY_UUID, "definition_version": version,
            "definition_history": "recorded", "response_sequence": sequence,
            "submitted_at": datetime(2026, 9, 1, 8, sequence, tzinfo=dt_timezone.utc).isoformat(),
            "consent_follow_up": consent, "is_complete": True, "voided_at": None,
            "respondent": {"cloud_user_ref": f"ref-{sequence}", "name": "王小明", "email": "c@example.com"},
            "answers": answers, "answers_format": 2}


class FakeInboxClient:
    def __init__(self, pages, ack_status="acked", revisions=None):
        self.pages, self.ack_status, self.revisions = list(pages), ack_status, revisions or {}
        self.posts = []

    def get(self, path, params=None):
        if path == "inbox/":
            return self.pages.pop(0) if self.pages else {"items": [], "has_more": False}
        if "/revisions/" in path:
            version = int(path.rstrip("/").rsplit("/", 1)[-1])
            if version in self.revisions:
                return {"definition": self.revisions[version]}
            raise CloudError(CLIENT)
        raise AssertionError(path)

    def post(self, path, body=None):
        self.posts.append((path, body))
        status = self.ack_status if path == "inbox/ack/" else "quarantined"
        return {"results": [{"submission_uuid": item["submission_uuid"], "status": status} for item in body["items"]]}


class IntakeTests(TestCase):
    def setUp(self):
        upsert_definition(definition(1))

    def test_written_submission_keeps_original_fields_and_raw_answers(self):
        env = envelope(1, U1, {Q1: "好", Q2: ["c1", "c2"]}, consent=True)
        self.assertEqual(intake(env, FakeInboxClient([])), "written")
        sub = FeedbackSubmission.objects.get(idempotency_key=U1)
        self.assertEqual((sub.submitted_at.isoformat(), sub.user, sub.respondent_ref), (env["submitted_at"], None, "ref-1"))
        self.assertEqual((sub.respondent_name, sub.consent_follow_up), ("王小明", True))
        choice_answer = Answer.objects.get(submission=sub, question__uuid=Q2)
        self.assertEqual((choice_answer.value, choice_answer.choice_codes), ("甲, 乙", ["c1", "c2"]))
        self.assertEqual(sub.synced_source.original_answers[Q2], ["c1", "c2"])
        self.assertEqual(sub.synced_source.payload_hash, envelope_payload_hash(env))
        self.assertTrue(PendingAck.objects.filter(submission_uuid=U1).exists())
        self.assertEqual(SurveySyncState.objects.get().synced_through_sequence, 1)

    def test_duplicate_and_conflict(self):
        intake(envelope(1, U1, {Q1: "好"}), FakeInboxClient([]))
        PendingAck.objects.all().delete()
        self.assertEqual(intake(envelope(1, U1, {Q1: "好"}), FakeInboxClient([])), "duplicate")
        self.assertTrue(PendingAck.objects.exists())
        self.assertEqual(intake(envelope(1, U1, {Q1: "差"}), FakeInboxClient([])), "content_conflict")
        self.assertEqual(Answer.objects.get().value, "好")

    def test_missing_revision_is_fetched_or_quarantined(self):
        newer = definition(2, [question(Q1), question(Q2, kind="multiple_choice", order=2), question(Q3, order=3)])
        self.assertEqual(intake(envelope(1, U1, {Q3: "x"}, version=2), FakeInboxClient([], revisions={2: newer})), "written")
        self.assertEqual(intake(envelope(2, U2, {Q1: "x"}, version=3), FakeInboxClient([])), "definition_unavailable")
        self.assertFalse(FeedbackSubmission.objects.filter(idempotency_key=U2).exists())

    def test_watermark_waits_for_gaps_and_abandoned(self):
        intake(envelope(1, U1, {Q1: "a"}), FakeInboxClient([]))
        intake(envelope(3, U3, {Q1: "c"}), FakeInboxClient([]))
        self.assertEqual(SurveySyncState.objects.get().synced_through_sequence, 1)
        state = SurveySyncState.objects.get()
        state.abandoned_sequences = [2]
        state.save()
        self.assertEqual(SurveySyncState.advance(state.survey), 3)
        self.assertEqual(SurveySyncState.objects.get().abandoned_sequences, [])


class SyncInboxTests(TestCase):
    def setUp(self):
        upsert_definition(definition(1))

    def test_only_acked_items_drop_their_pending_ack(self):
        client = FakeInboxClient([{"items": [envelope(1, U1, {Q1: "a"})], "has_more": False}], ack_status="conflict")
        sync_inbox(client)
        self.assertEqual(PendingAck.objects.get().last_status, "conflict")

    def test_leftover_pending_ack_is_resent_first(self):
        PendingAck.objects.create(submission_uuid=U9, payload_hash="h" * 64)
        client = FakeInboxClient([{"items": [], "has_more": False}])
        sync_inbox(client)
        self.assertEqual(client.posts[0][0], "inbox/ack/")
        self.assertFalse(PendingAck.objects.exists())

    def test_quarantine_reason_is_reported_and_not_acked(self):
        client = FakeInboxClient([{"items": [envelope(1, U1, {Q1: "a"}, version=9)], "has_more": False}])
        result = sync_inbox(client)
        self.assertEqual(result.quarantined, 1)
        self.assertEqual([path for path, _ in client.posts], ["inbox/quarantine/"])

    def test_unacknowledged_pages_do_not_loop_forever(self):
        page = {"items": [envelope(1, U1, {Q1: "a"})], "has_more": True}
        client = FakeInboxClient([page, page, page], ack_status="conflict")
        sync_inbox(client)
        self.assertEqual(len(client.pages), 2)  # stopped after the page that made no progress

    def test_new_submissions_schedule_local_analysis_once(self):
        page = {"items": [envelope(1, U1, {Q1: "a"}), envelope(2, U2, {Q1: "b"})], "has_more": False}
        sync_inbox(FakeInboxClient([page]))
        survey = Survey.objects.get()
        self.assertEqual(AnalysisJob.objects.filter(survey=survey, status="pending").count(), 1)


class AnswersFormatIntakeTests(TestCase):
    """Builder spec §2.3, §7.3: choice answers travel as codes inside answers_format 2."""

    def setUp(self):
        upsert_definition(definition(1))

    def test_node_quarantines_missing_answers_format(self):
        env = envelope(1, U1, {Q1: "好"})
        env.pop("answers_format")
        self.assertEqual(intake(env, FakeInboxClient([])), "answers_format")
        self.assertFalse(FeedbackSubmission.objects.exists())

    def test_node_quarantines_answers_format_other_than_2(self):
        env = envelope(1, U1, {Q1: "好"})
        env["answers_format"] = 1
        self.assertEqual(intake(env, FakeInboxClient([])), "answers_format")

    def test_unknown_choice_code_quarantined(self):
        self.assertEqual(intake(envelope(1, U1, {Q2: ["c9"]}), FakeInboxClient([])), "answers_format")
        self.assertFalse(FeedbackSubmission.objects.exists())

    def test_ack_after_codes_written(self):
        env = envelope(1, U1, {Q2: ["c2"]})
        self.assertEqual(intake(env, FakeInboxClient([])), "written")
        self.assertEqual(PendingAck.objects.get(submission_uuid=U1).payload_hash, envelope_payload_hash(env))

    def test_capture_uses_analysis_definition_version(self):
        from cloudsync.capture import capture_scope

        survey = Survey.objects.get(uuid=SURVEY_UUID)
        self.assertEqual(capture_scope(survey).definition_version, survey.analysis_definition_version)
