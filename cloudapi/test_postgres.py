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
