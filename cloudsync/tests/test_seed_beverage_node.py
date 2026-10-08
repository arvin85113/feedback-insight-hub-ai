"""seed_demo_beverage node paths against a real cloud subprocess (node-only analysis spec §4)."""

import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from cloudsync.models import CloudLink
from cloudsync.testing import CloudServer
from cloudsync.tests.utils import memory_keyring
from cloudsync.tokens import save_token
from feedback.management.commands.seed_demo_beverage import BEVERAGE_NODE_SURVEY_UUID
from feedback.models import KeywordCategory, Survey

CLOUD_SURVEY = """
from feedback.models import Survey
s = Survey.objects.get(uuid='{uuid}')
print(s.owner_node.name, s.published_version is not None)
"""


class NodeBeverageSeedTests(TestCase):
    def setUp(self):
        # Windows may keep the terminated server's SQLite handle briefly; leftover temp files are harmless.
        workdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(workdir.cleanup)
        self.cloud = CloudServer(Path(workdir.name), inbox=True, self_test_surveys=[BEVERAGE_NODE_SURVEY_UUID])
        self.cloud.__enter__()
        self.addCleanup(self.cloud.__exit__, None, None, None)
        keyring = memory_keyring()
        keyring.__enter__()
        self.addCleanup(keyring.__exit__, None, None, None)
        loopback = override_settings(CLOUD_SYNC_ALLOW_LOOPBACK_HTTP=True)
        loopback.enable()
        self.addCleanup(loopback.disable)
        CloudLink.relink(self.cloud.url, self.cloud.node_uuid)
        save_token(self.cloud.url, self.cloud.token)

    def node_create(self):
        call_command("seed_demo_beverage", "--node-create", stdout=StringIO())
        return Survey.objects.get(uuid=BEVERAGE_NODE_SURVEY_UUID)

    def test_node_create_publishes_owned_survey_with_keywords(self):
        survey = self.node_create()
        self.assertIsNotNone(survey.published_version)
        self.assertEqual([q.kind for q in survey.questions.order_by("order")],
                         ["single_choice", "single_choice", "multiple_choice", "scale", "scale", "single_choice",
                          "decimal", "decimal", "integer", "long_text"])
        self.assertEqual(KeywordCategory.objects.filter(survey=survey).count(), 6)
        cloud = self.cloud.shell(CLOUD_SURVEY.format(uuid=BEVERAGE_NODE_SURVEY_UUID)).strip().splitlines()[-1]
        self.assertEqual(cloud.split(), ["e2e", "True"])

    def test_node_create_requires_cloud_link(self):
        CloudLink.unlink()
        with self.assertRaisesMessage(CommandError, "尚未連結雲端"):
            call_command("seed_demo_beverage", "--node-create", stdout=StringIO())
        self.assertFalse(Survey.objects.filter(uuid=BEVERAGE_NODE_SURVEY_UUID).exists())
        self.assertFalse(KeywordCategory.objects.exists())

    def test_node_create_twice_refuses(self):
        self.node_create()
        with self.assertRaisesMessage(CommandError, "已存在"):
            self.node_create()
