from django.test import TestCase, override_settings

from cloudapi.models import ChangeClock, NodeDevice, RevisionImmutable, SurveyDefinitionRevision
from feedback.models import Question, Survey


class NodeDeviceTests(TestCase):
    def test_issue_stores_only_the_hash(self):
        device, token = NodeDevice.issue("office")
        self.assertTrue(token.startswith("fih_"))
        self.assertNotEqual(device.token_hash, token)
        self.assertEqual(device.token_hash, NodeDevice.hash_token(token))
        self.assertEqual(device.status, NodeDevice.Status.ACTIVE)

    def test_rotate_invalidates_the_old_token(self):
        device, old = NodeDevice.issue("office")
        new = device.rotate()
        device.refresh_from_db()
        self.assertNotEqual(old, new)
        self.assertEqual(device.token_hash, NodeDevice.hash_token(new))

    def test_rotate_command_prints_token_once_and_admin_cannot_rotate(self):
        import io

        from django.contrib import admin as django_admin
        from django.core.management import call_command

        device, _ = NodeDevice.issue("office")
        out = io.StringIO()
        call_command("rotate_node_token", name="office", stdout=out)
        token = out.getvalue().strip().splitlines()[-1]
        device.refresh_from_db()
        self.assertEqual(device.token_hash, NodeDevice.hash_token(token))
        model_admin = django_admin.site._registry[NodeDevice]
        self.assertEqual(tuple(model_admin.actions), ("revoke",))


class SharedFieldTests(TestCase):
    def test_new_rows_get_distinct_uuids_and_defaults(self):
        first = Survey.objects.create(title="A", slug="a")
        second = Survey.objects.create(title="B", slug="b")
        self.assertNotEqual(first.uuid, second.uuid)
        self.assertEqual(first.definition_version, 0)
        self.assertIsNone(first.owner_node)
        question = Question.objects.create(survey=first, title="Q", kind="short_text", data_type="text")
        self.assertFalse(question.has_received_answer)
        self.assertIsNotNone(question.uuid)

    def test_change_clock_row_exists(self):
        clock = ChangeClock.objects.get(pk=1)
        self.assertEqual((clock.value, clock.pruned_through), (0, 0))


class RevisionTests(TestCase):
    def test_revisions_are_immutable(self):
        survey = Survey.objects.create(title="A", slug="a")
        revision = SurveyDefinitionRevision.objects.create(survey=survey, version=1, definition={"title": "A"})
        revision.definition = {"title": "changed"}
        with self.assertRaises(RevisionImmutable):
            revision.save()
