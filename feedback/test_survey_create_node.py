"""Website drafts belong to the connected node (node-only analysis spec §5)."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from cloudapi.models import NodeDevice, SurveyDefinitionRevision
from feedback.models import Survey
from feedback.test_utils import cloud_only
from feedback.views import NODE_MISSING_NOTICE


@cloud_only
@override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=True)
class SurveyCreateNodeTests(TestCase):
    def setUp(self):
        manager = get_user_model().objects.create_user(username="m", password="x", role="manager")
        self.client.force_login(manager)

    def create(self, title="新問卷"):
        return self.client.post(reverse("feedback:survey-create"), {"title": title}, follow=True)

    def test_one_active_node_owns_new_draft(self):
        node, _ = NodeDevice.issue("office")
        self.create()
        survey = Survey.objects.get(title="新問卷")
        self.assertEqual(survey.owner_node, node)
        self.assertIsNone(survey.published_version)
        latest = SurveyDefinitionRevision.objects.filter(survey=survey).order_by("-version").first()
        self.assertEqual(latest.version, survey.definition_version)

    def test_no_node_creates_unowned_draft_with_notice(self):
        self.assertContains(self.client.get(reverse("feedback:survey-create")), NODE_MISSING_NOTICE)
        self.create()
        survey = Survey.objects.get(title="新問卷")
        self.assertIsNone(survey.owner_node)
        self.assertContains(self.client.get(reverse("feedback:survey-builder", args=[survey.slug])), NODE_MISSING_NOTICE)

    def test_two_active_nodes_block_creation(self):
        NodeDevice.issue("office")
        NodeDevice.issue("branch")
        self.assertContains(self.create(), "多個本機節點")
        self.assertFalse(Survey.objects.exists())

    def test_revoked_node_is_not_counted(self):
        node, _ = NodeDevice.issue("office")
        revoked, _ = NodeDevice.issue("old")
        NodeDevice.objects.filter(pk=revoked.pk).update(status=NodeDevice.Status.REVOKED)
        self.create()
        self.assertEqual(Survey.objects.get(title="新問卷").owner_node, node)

    @override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=False)
    def test_prototype_off_keeps_current_behaviour(self):
        NodeDevice.issue("office")
        self.assertNotContains(self.client.get(reverse("feedback:survey-create")), NODE_MISSING_NOTICE)
        self.create()
        self.assertIsNone(Survey.objects.get(title="新問卷").owner_node)
