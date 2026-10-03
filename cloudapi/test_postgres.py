import threading
import time
from unittest import SkipTest

from django.db import close_old_connections, connection, connections, transaction
from django.test import TransactionTestCase, override_settings

from cloudapi.definition import serialize_definition, update_survey
from cloudapi.models import ChangeClock, NodeDevice, SurveyChange
from cloudapi.writes import change_definition, create_node_survey


def blank(index):
    return {"survey_uuid": f"00000000-0000-0000-0000-00000000000{index}", "version": 0, "title": f"S{index}",
            "slug": "", "description": "", "is_active": True, "analysis_enabled": True,
            "thank_you_email_enabled": True, "improvement_tracking_enabled": True, "category": None,
            "archived_at": None, "questions": []}


def visible_seqs():
    """Read SurveyChange through a fresh connection, i.e. what another client sees right now."""

    observer = connections.create_connection("default")
    try:
        with observer.cursor() as cursor:
            cursor.execute(f"SELECT seq FROM {SurveyChange._meta.db_table} ORDER BY seq")
            return [row[0] for row in cursor.fetchall()]
    finally:
        observer.close()


class ChangeSequencePostgreSQLTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        if connection.vendor != "postgresql":
            raise SkipTest("需要使用隔離 PostgreSQL 執行")
        super().setUpClass()

    def setUp(self):
        ChangeClock.objects.get_or_create(pk=1)
        node, _ = NodeDevice.issue("pg")
        self.first = create_node_survey(node, blank(1))[0].survey
        self.second = create_node_survey(node, blank(2))[0].survey
        self.baseline = visible_seqs()  # seqs 1 and 2 from the two creates

    def edit(self, survey, *, hold=None, entered=None, errors):
        try:
            definition = serialize_definition(survey)
            update_survey(definition, {"title": survey.title + "!"})
            with transaction.atomic():
                change_definition(survey.uuid, expected_version=1, definition=definition)
                if entered:
                    entered.set()
                if hold:
                    hold.wait(10)  # keep the transaction (and the clock row lock) open
        except Exception as exc:  # pragma: no cover - reported by the test
            errors.append(exc)
        finally:
            close_old_connections()

    def test_no_later_sequence_becomes_visible_before_an_earlier_one_commits(self):
        hold, entered, errors = threading.Event(), threading.Event(), []
        first = threading.Thread(target=self.edit, args=(self.first,), kwargs={"hold": hold, "entered": entered, "errors": errors})
        first.start()
        self.assertTrue(entered.wait(10))  # first has taken seq 3 and is still uncommitted

        second = threading.Thread(target=self.edit, args=(self.second,), kwargs={"errors": errors})
        second.start()
        time.sleep(0.5)
        # The second writer must be blocked on the clock row, so nothing new is visible yet.
        self.assertTrue(second.is_alive())
        self.assertEqual(visible_seqs(), self.baseline)

        hold.set()
        first.join(10)
        second.join(10)
        self.assertEqual(errors, [])
        seqs = visible_seqs()
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        by_survey = dict(SurveyChange.objects.filter(seq__gt=2).values_list("survey_id", "seq"))
        self.assertLess(by_survey[self.first.pk], by_survey[self.second.pk])


class _InboxPostgreSQLCase(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        if connection.vendor != "postgresql":
            raise SkipTest("需要使用隔離 PostgreSQL 執行")
        super().setUpClass()

    def make_inbox_survey(self, kind="short_text", data_type="text", options_text=""):
        from django.utils import timezone

        from cloudapi.writes import assign_survey_to_node
        from feedback.models import Question, Survey

        ChangeClock.objects.get_or_create(pk=1)
        node, _ = NodeDevice.issue("pg-inbox")
        survey = Survey.objects.create(title="S", slug="pg-inbox")
        question = Question.objects.create(survey=survey, title="Q", kind=kind, data_type=data_type,
                                           options_text=options_text, order=1)
        survey = assign_survey_to_node(survey, node).survey
        Survey.objects.filter(pk=survey.pk).update(inbox_since=timezone.now())
        survey.refresh_from_db()
        return survey, question


@override_settings(CLOUD_INBOX_ENABLED=True, CLOUD_INBOX_MAX_COUNT=3)
class InboxCapacityPostgreSQLTests(_InboxPostgreSQLCase):
    def test_concurrent_accepts_never_exceed_the_limit(self):
        import uuid

        from django.contrib.auth import get_user_model

        from cloudapi.inbox import InboxFull, accept_submission
        from cloudapi.models import InboxCounter, SubmissionReceipt

        survey, question = self.make_inbox_survey()
        users = [get_user_model().objects.create_user(username=f"u{i}", password="x") for i in range(8)]
        barrier, full, errors = threading.Barrier(8), [], []

        def submit(user):
            try:
                barrier.wait(10)
                accept_submission(survey, user=user, submission_uuid=uuid.uuid4(), form_version=1,
                                  consent_follow_up=False, answers={str(question.uuid): "x"})
            except InboxFull:
                full.append(1)
            except Exception as exc:  # pragma: no cover - reported by the test
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=submit, args=(user,)) for user in users]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(full), 5)
        self.assertEqual(sorted(SubmissionReceipt.objects.values_list("response_sequence", flat=True)), [1, 2, 3])
        self.assertEqual(InboxCounter.objects.get().occupied_count, 3)


@override_settings(CLOUD_INBOX_ENABLED=True)
class SemanticLockRacePostgreSQLTests(_InboxPostgreSQLCase):
    def test_first_answer_and_semantic_edit_are_serialized(self):
        import uuid

        from django.contrib.auth import get_user_model

        from cloudapi.definition import serialize_definition, update_question
        from cloudapi.errors import SemanticLockViolation
        from cloudapi.inbox import DefinitionOutdated, accept_submission
        from cloudapi.models import SubmissionReceipt
        from feedback.models import Question

        survey, question = self.make_inbox_survey("single_choice", "nominal", "A\nB")
        user = get_user_model().objects.create_user(username="racer", password="x")
        definition = serialize_definition(survey)
        update_question(definition, str(question.uuid), {"options_text": "A\nB\nC"})
        barrier, outcomes = threading.Barrier(2), {}

        def answer():
            try:
                barrier.wait(10)
                accept_submission(survey, user=user, submission_uuid=uuid.uuid4(), form_version=1,
                                  consent_follow_up=False, answers={str(question.uuid): "A"})
                outcomes["answer"] = "ok"
            except DefinitionOutdated:
                outcomes["answer"] = "outdated"
            finally:
                close_old_connections()

        def edit():
            try:
                barrier.wait(10)
                change_definition(survey.uuid, expected_version=1, definition=definition)
                outcomes["edit"] = "ok"
            except SemanticLockViolation:
                outcomes["edit"] = "locked"
            finally:
                close_old_connections()

        threads = [threading.Thread(target=answer), threading.Thread(target=edit)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        question.refresh_from_db()
        if outcomes == {"answer": "ok", "edit": "locked"}:
            self.assertEqual((question.options_text, question.has_received_answer), ("A\nB", True))
        else:
            self.assertEqual(outcomes, {"answer": "outdated", "edit": "ok"})
            self.assertFalse(SubmissionReceipt.objects.exists())
            self.assertFalse(question.has_received_answer)


@override_settings(CLOUD_INBOX_ENABLED=True)
class ConcurrentAckPostgreSQLTests(_InboxPostgreSQLCase):
    def test_two_acks_for_one_item_ack_once(self):
        import uuid

        from django.contrib.auth import get_user_model

        from cloudapi.inbox import accept_submission, ack_items
        from cloudapi.models import InboxCounter

        survey, question = self.make_inbox_survey()
        user = get_user_model().objects.create_user(username="acker", password="x")
        receipt = accept_submission(survey, user=user, submission_uuid=uuid.uuid4(), form_version=1,
                                    consent_follow_up=False, answers={str(question.uuid): "x"}).receipt
        item = {"submission_uuid": str(receipt.submission_uuid), "payload_hash": receipt.payload_hash}
        barrier, statuses = threading.Barrier(2), []

        def ack():
            try:
                barrier.wait(10)
                statuses.append(ack_items(survey.owner_node, [item])[0]["status"])
            finally:
                close_old_connections()

        threads = [threading.Thread(target=ack) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(sorted(statuses), ["acked", "already_acked"])
        counter = InboxCounter.objects.get()
        self.assertEqual((counter.occupied_count, counter.occupied_bytes), (0, 0))


class ConcurrentResultPostgreSQLTests(_InboxPostgreSQLCase):
    def test_concurrent_uploads_are_serialized(self):
        import uuid
        from unittest.mock import patch

        from cloudapi.envelope import sha256_hex
        from cloudapi.models import PublishedResultRecord
        from cloudapi.results import apply_upload
        from cloudapi.tests.test_results import content
        from feedback.models import Survey, SurveyAnalysisState

        survey, _question = self.make_inbox_survey()
        Survey.objects.filter(pk=survey.pk).update(response_sequence=5)
        uuid12, uuid11 = uuid.uuid4(), uuid.uuid4()
        holding, release, errors = threading.Event(), threading.Event(), []
        original_create = PublishedResultRecord.objects.create

        def create_then_hold(**kwargs):
            record = original_create(**kwargs)
            if kwargs["publish_uuid"] == uuid12:
                holding.set()
                release.wait(10)  # thread A keeps the SurveyAnalysisState row lock here
            return record

        def upload(sequence, publish_uuid, title):
            try:
                body = content(survey, title=title)
                apply_upload(survey.owner_node, publish_uuid=str(publish_uuid), publish_sequence=sequence,
                             content_hash=sha256_hex(body), content=body)
            except Exception as exc:  # pragma: no cover - reported by the test
                errors.append(exc)
            finally:
                close_old_connections()

        with patch.object(PublishedResultRecord.objects, "create", side_effect=create_then_hold):
            first = threading.Thread(target=upload, args=(12, uuid12, "twelve"))
            first.start()
            self.assertTrue(holding.wait(10))
            second = threading.Thread(target=upload, args=(11, uuid11, "eleven"))
            second.start()
            time.sleep(0.5)
            self.assertTrue(second.is_alive())  # blocked on the state row lock
            release.set()
            first.join(30)
            second.join(30)
        self.assertEqual(errors, [])
        state = SurveyAnalysisState.objects.get(survey=survey)
        self.assertEqual((state.publish_sequence, state.published_upload_uuid), (12, uuid12))
        self.assertEqual(state.published_display_payload["statistics"]["title"], "twelve")
        self.assertFalse(PublishedResultRecord.objects.get(publish_uuid=uuid11).applied)


class PurgeInboxLockOrderPostgreSQLTests(TransactionTestCase):
    """purge, ACK and abandon share one lock order (receipt, body, counter), so none of them deadlocks (spec §7.4)."""

    @classmethod
    def setUpClass(cls):
        if connection.vendor != "postgresql":
            raise SkipTest("需要使用隔離 PostgreSQL 執行")
        super().setUpClass()

    def setUp(self):
        import uuid as uuid_module

        from django.utils import timezone

        from cloudapi.models import InboxCounter, InboxSubmission, SubmissionReceipt
        from feedback.models import Survey

        self.node, _ = NodeDevice.issue("pg-purge")
        self.survey = Survey.objects.create(title="purge", slug="purge-pg", owner_node=self.node)
        self.uid = uuid_module.uuid4()
        state = InboxSubmission.State.QUARANTINED if self.quarantined else InboxSubmission.State.PENDING
        InboxSubmission.objects.create(submission_uuid=self.uid, node=self.node, survey=self.survey, envelope={},
                                       answers_hash="a" * 64, payload_hash="a" * 64, size_bytes=100, state=state)
        self.receipt = SubmissionReceipt.objects.create(
            submission_uuid=self.uid, node=self.node, survey=self.survey, submitted_at=timezone.now(),
            definition_version=1, response_sequence=1, payload_hash="a" * 64,
            status=SubmissionReceipt.Status.QUARANTINED if self.quarantined else SubmissionReceipt.Status.RECEIVED,
        )
        InboxCounter.objects.update_or_create(node=self.node, defaults={"occupied_count": 1, "occupied_bytes": 100})
        Survey.objects.filter(pk=self.survey.pk).update(owner_node=None)

    quarantined = False

    def race(self, other):
        """Purge holds the receipt locks; `other` must queue behind them instead of grabbing the body first."""

        from unittest.mock import patch

        from django.core.exceptions import ObjectDoesNotExist

        import feedback.survey_purge as purge_module

        from feedback.models import Survey

        holding, release = threading.Event(), threading.Event()
        purge_errors, competitor_errors = [], []
        original = purge_module._lock_receipts

        def lock_then_hold(survey):
            original(survey)
            holding.set()
            release.wait(10)

        def run(target, errors, *, allowed=()):
            try:
                target()
            except allowed:
                pass  # losing the race to purge (the row is already gone) is an allowed outcome
            except Exception as exc:  # a deadlock abort (OperationalError) or any purge failure lands here
                errors.append(exc)
            finally:
                close_old_connections()

        with patch.object(purge_module, "_lock_receipts", side_effect=lock_then_hold):
            purger = threading.Thread(target=run, args=(lambda: purge_module.purge_survey(self.survey), purge_errors))
            purger.start()
            self.assertTrue(holding.wait(10))
            competitor = threading.Thread(target=run, args=(other, competitor_errors),
                                          kwargs={"allowed": (ObjectDoesNotExist,)})
            competitor.start()
            time.sleep(0.5)
            self.assertTrue(competitor.is_alive())  # queued on the receipt lock
            release.set()
            purger.join(30)
            competitor.join(30)
        self.assertFalse(purger.is_alive() or competitor.is_alive())
        self.assertEqual((purge_errors, competitor_errors), ([], []))
        self.assertFalse(Survey.objects.filter(pk=self.survey.pk).exists())

    def assert_capacity_matches_bodies(self):
        from django.db.models import Sum

        from cloudapi.models import InboxCounter, InboxSubmission

        counter = InboxCounter.objects.get(node=self.node)
        bodies = InboxSubmission.objects.filter(node=self.node)
        self.assertEqual((counter.occupied_count, counter.occupied_bytes),
                         (bodies.count(), bodies.aggregate(total=Sum("size_bytes"))["total"] or 0))

    def test_purge_and_ack_do_not_double_release_capacity(self):
        from cloudapi.inbox import ack_items

        self.race(lambda: ack_items(self.node, [{"submission_uuid": str(self.uid), "payload_hash": "a" * 64}]))
        self.assert_capacity_matches_bodies()


class PurgeAbandonLockOrderPostgreSQLTests(PurgeInboxLockOrderPostgreSQLTests):
    quarantined = True

    def test_purge_and_ack_do_not_double_release_capacity(self):
        self.skipTest("covered by the parent class")

    def test_purge_and_abandon_do_not_double_release_capacity(self):
        from cloudapi.inbox import abandon

        self.race(lambda: abandon(self.receipt, None))
        self.assert_capacity_matches_bodies()


class PurgeQuarantineLockOrderPostgreSQLTests(PurgeInboxLockOrderPostgreSQLTests):
    def test_purge_and_ack_do_not_double_release_capacity(self):
        self.skipTest("covered by the parent class")

    def test_purge_and_quarantine_do_not_deadlock(self):
        from cloudapi.inbox import quarantine_items

        self.race(lambda: quarantine_items(self.node, [{"submission_uuid": str(self.uid), "reason": "content_conflict"}]))
        self.assert_capacity_matches_bodies()
