import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings

from cloudapi.definition import add_question, serialize_definition, update_question
from cloudapi.errors import SemanticLockViolation, VersionConflict
from cloudsync.models import CloudLink
from cloudsync.runner import run_cycle
from cloudsync.survey_write import node_commit
from cloudsync.testing import CloudServer
from cloudsync.tests.utils import memory_keyring
from cloudsync.tokens import save_token
from feedback.models import Answer, FeedbackSubmission, Question, Survey

QUESTION = {"title": "滿意度", "help_text": "", "kind": "single_choice", "data_type": "ordinal",
            "options_text": "好\n普通\n差", "is_required": True, "enable_keyword_tracking": False, "order": 2}

SEED = """
from cloudapi.models import NodeDevice
from cloudapi.writes import assign_survey_to_node
from feedback.models import Question, Survey
survey = Survey.objects.create(title="門市問卷", slug="{slug}")
Question.objects.create(survey=survey, title="感想", kind="long_text", data_type="text", order=1)
assign_survey_to_node(survey, NodeDevice.objects.get(name="e2e"))
print(survey.uuid)
"""


class DefinitionSyncEndToEndTests(TestCase):
    """Both tests share one cloud subprocess, so each seeds its own survey (version 1) on the cloud."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Windows may keep the terminated server's SQLite handle briefly; leftover temp files are harmless.
        cls._workdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.cloud = CloudServer(Path(cls._workdir.name))
        cls.cloud.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.cloud.__exit__(None, None, None)
        cls._workdir.cleanup()
        super().tearDownClass()

    def setUp(self):
        self._keyring = memory_keyring()
        self._keyring.__enter__()
        self.addCleanup(self._keyring.__exit__, None, None, None)
        # The isolated cloud subprocess listens on plain HTTP loopback; only this test opts in.
        loopback = override_settings(CLOUD_SYNC_ALLOW_LOOPBACK_HTTP=True)
        loopback.enable()
        self.addCleanup(loopback.disable)
        CloudLink.relink(self.cloud.url, self.cloud.node_uuid)
        save_token(self.cloud.url, self.cloud.token)
        slug = self._testMethodName.replace("_", "-")[:40]
        self.survey_uuid = self.cloud.shell(SEED.replace("{slug}", slug)).strip().splitlines()[-1]

    def test_round_trip_conflict_semantic_lock_and_deactivation(self):
        self.assertEqual(run_cycle(force=True), "ok")
        survey = Survey.objects.get(uuid=self.survey_uuid)
        self.assertEqual(survey.definition_version, 1)

        # Node adds a question through the cloud.
        definition = serialize_definition(survey)
        add_question(definition, QUESTION)
        node_commit(survey, definition, 1)
        survey.refresh_from_db()
        self.assertEqual(survey.definition_version, 2)
        self.assertIn("滿意度", self.cloud.shell(
            f"from feedback.models import Survey; print([q.title for q in Survey.objects.get(uuid='{self.survey_uuid}').questions.all()])"))

        # A cloud-side edit makes the node's next write stale.
        self.cloud.shell(f"""
from cloudapi.definition import serialize_definition, update_survey
from cloudapi.writes import change_definition
from feedback.models import Survey
s = Survey.objects.get(uuid='{self.survey_uuid}')
d = serialize_definition(s); update_survey(d, {{"title": "雲端改名"}})
change_definition(s.uuid, expected_version=2, definition=d)
""")
        stale = serialize_definition(survey)
        stale["description"] = "本機的修改"
        with self.assertRaises(VersionConflict):
            node_commit(survey, stale, 2)
        run_cycle(force=True)
        survey.refresh_from_db()
        self.assertEqual((survey.title, survey.definition_version), ("雲端改名", 3))

        # Semantic lock on an answered question.
        self.cloud.shell(f"from feedback.models import Question; Question.objects.filter(survey__uuid='{self.survey_uuid}', title='滿意度').update(has_received_answer=True)")
        locked = serialize_definition(survey)
        rated = next(q for q in locked["questions"] if q["title"] == "滿意度")
        update_question(locked, rated["uuid"], {"options_text": "好\n差"})
        with self.assertRaises(SemanticLockViolation):
            node_commit(survey, locked, 3)

        # Cloud deactivates the free-text question; the node keeps its answers.
        free_text = Question.objects.get(survey=survey, title="感想")
        Answer.objects.create(submission=FeedbackSubmission.objects.create(survey=survey), question=free_text, value="好")
        self.cloud.shell(f"""
from cloudapi.definition import serialize_definition, set_question_active
from cloudapi.writes import change_definition
from feedback.models import Survey
s = Survey.objects.get(uuid='{self.survey_uuid}')
d = serialize_definition(s)
set_question_active(d, next(q['uuid'] for q in d['questions'] if q['title'] == '感想'), False)
change_definition(s.uuid, expected_version=3, definition=d)
""")
        run_cycle(force=True)
        free_text.refresh_from_db()
        self.assertFalse(free_text.is_active)
        self.assertEqual(Answer.objects.filter(question=free_text).count(), 1)

    def test_interrupted_sync_resumes_without_loss(self):
        run_cycle(force=True)
        cursor_before = CloudLink.load().cursor
        with patch("cloudsync.definitions.upsert_definition", side_effect=RuntimeError("crash")):
            self.cloud.shell(f"""
from cloudapi.definition import serialize_definition, update_survey
from cloudapi.writes import change_definition
from feedback.models import Survey
s = Survey.objects.get(uuid='{self.survey_uuid}')
d = serialize_definition(s); update_survey(d, {{"description": "第二次修改"}})
change_definition(s.uuid, expected_version=s.definition_version, definition=d)
""")
            with self.assertRaises(RuntimeError):
                run_cycle(force=True)
        self.assertEqual(CloudLink.load().cursor, cursor_before)
        self.assertEqual(run_cycle(force=True), "ok")
        self.assertEqual(Survey.objects.get(uuid=self.survey_uuid).description, "第二次修改")
