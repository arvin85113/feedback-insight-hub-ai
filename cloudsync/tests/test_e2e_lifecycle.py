"""Node-side survey lifecycle against a real cloud subprocess (builder spec §4.4, §7.1, §7.4)."""

import tempfile
from pathlib import Path

from django.test import TestCase, override_settings

from cloudapi.definition import add_question, serialize_definition
from cloudsync.models import CloudLink, SurveySyncState
from cloudsync.testing import CloudServer
from cloudsync.tests.utils import memory_keyring
from cloudsync.tokens import save_token
from feedback import survey_lifecycle
from feedback.models import Survey


class NodeLifecycleEndToEndTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
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
        loopback = override_settings(CLOUD_SYNC_ALLOW_LOOPBACK_HTTP=True)
        loopback.enable()
        self.addCleanup(loopback.disable)
        CloudLink.relink(self.cloud.url, self.cloud.node_uuid)
        save_token(self.cloud.url, self.cloud.token)

    def cloud_value(self, survey_uuid, expression):
        return self.cloud.shell(
            f"from feedback.models import Survey; s = Survey.objects.get(uuid='{survey_uuid}'); print({expression})"
        ).strip().splitlines()[-1]

    def node_draft(self, title):
        survey = survey_lifecycle.create_draft({"title": title})
        definition = serialize_definition(survey)
        add_question(definition, {"title": "門市", "kind": "single_choice",
                                  "choices": [{"code": "", "label": "信義"}, {"code": "", "label": "公館"}]})
        survey_lifecycle.commit(survey, definition, survey.definition_version)
        return Survey.objects.get(pk=survey.pk)

    def test_node_copy_belongs_to_node_and_is_draft(self):
        original = self.node_draft("本機草稿")
        copy = survey_lifecycle.copy_as_draft(original)
        self.assertNotEqual(copy.uuid, original.uuid)
        self.assertEqual((copy.definition_version, copy.published_version), (1, None))
        self.assertEqual([q.code for q in copy.questions.all()], [q.code for q in original.questions.all()])
        self.assertEqual(self.cloud_value(copy.uuid, "s.owner_node.name"), "e2e")
        self.assertTrue(SurveySyncState.objects.filter(survey=copy).exists())

    def test_node_delete_right_after_create_or_copy_goes_to_cloud(self):
        created = survey_lifecycle.create_draft({"title": "剛建立"})
        self.assertTrue(SurveySyncState.objects.filter(survey=created).exists())
        self.assertEqual(survey_lifecycle.delete_or_archive(created, created.definition_version), "archived")
        self.assertTrue(Survey.objects.filter(pk=created.pk).exists())
        self.assertNotEqual(self.cloud_value(created.uuid, "s.archived_at"), "None")

        copy = survey_lifecycle.copy_as_draft(self.node_draft("原稿"))
        self.assertEqual(survey_lifecycle.delete_or_archive(copy, copy.definition_version), "archived")
        self.assertNotEqual(self.cloud_value(copy.uuid, "s.archived_at"), "None")
