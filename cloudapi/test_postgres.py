import threading
import time
from unittest import SkipTest

from django.db import close_old_connections, connection, connections, transaction
from django.test import TransactionTestCase

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
