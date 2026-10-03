import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from cloudapi.definition import serialize_definition
from cloudsync.client import CONFLICT, TRANSIENT, CloudError
from cloudsync.models import CloudLink
from cloudsync.survey_write import create_survey, node_commit
from cloudsync.tests.utils import memory_keyring
from cloudsync.tokens import load_token, save_token
from config.node_paths import NodePaths
from feedback.models import Question, Survey
from organizations.models import Organization, OrganizationMembership

User = get_user_model()


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def _call(self, method, path, body):
        self.calls.append((method, path, body))
        if self.error:
            raise self.error
        return self.response

    def put(self, path, body):
        return self._call("PUT", path, body)

    def post(self, path, body=None):
        return self._call("POST", path, body)


class NodePageCase(TestCase):
    def setUp(self):
        self._keyring = memory_keyring()
        self._keyring.__enter__()
        self.addCleanup(self._keyring.__exit__, None, None, None)
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        paths = NodePaths(Path(home.name))
        paths.ensure(restrict=lambda _path: None)
        override = override_settings(NODE_PATHS=paths)
        override.enable()
        self.addCleanup(override.disable)
        organization = Organization.objects.create(name="Acme")
        self.owner = User.objects.create_user(username="o@x.com", email="o@x.com", password="x", role=User.Role.MANAGER)
        OrganizationMembership.objects.create(user=self.owner, organization=organization, role="owner")
        self.client.force_login(self.owner)
        CloudLink.relink("https://c", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        save_token("https://c", "tok")
        self.survey = Survey.objects.create(title="S", slug="s", definition_version=1)
        self.question = Question.objects.create(survey=self.survey, title="Q", kind="short_text", data_type="text", order=1)


class NodeCommitTests(NodePageCase):
    def test_success_applies_the_returned_definition(self):
        definition = serialize_definition(self.survey)
        definition["title"] = "新名稱"
        returned = {**definition, "version": 2}
        with patch("cloudsync.survey_write.client_for_link", return_value=FakeClient({"definition": returned})):
            node_commit(self.survey, definition, 1)
        self.survey.refresh_from_db()
        self.assertEqual((self.survey.title, self.survey.definition_version), ("新名稱", 2))

    def test_offline_and_conflict_do_not_touch_the_local_copy(self):
        from cloudapi.errors import VersionConflict
        from cloudsync.survey_write import OfflineError

        definition = serialize_definition(self.survey)
        definition["title"] = "不該寫入"
        for error, expected in ((CloudError(TRANSIENT), OfflineError),
                                (CloudError(CONFLICT, payload={"current_version": 5}), VersionConflict)):
            with patch("cloudsync.survey_write.client_for_link", return_value=FakeClient(error=error)), \
                 self.assertRaises(expected):
                node_commit(self.survey, definition, 1)
        self.survey.refresh_from_db()
        self.assertEqual(self.survey.title, "S")


class NodeBuilderTests(NodePageCase):
    def test_builder_writes_through_the_api(self):
        returned = {**serialize_definition(self.survey), "version": 2}
        returned["questions"][0]["is_active"] = False
        fake = FakeClient({"definition": returned})
        with patch("cloudsync.survey_write.client_for_link", return_value=fake):
            self.client.post(reverse("feedback:survey-builder", args=["s"]), {
                "definition_version": 1, "action": "delete-question", "question_uuid": str(self.question.uuid),
                "question_id": self.question.pk,
            })
        self.assertEqual(fake.calls[0][0:2], ("PUT", f"surveys/{self.survey.uuid}/"))
        self.question.refresh_from_db()
        self.assertFalse(self.question.is_active)

    def test_not_linked_builder_is_read_only(self):
        CloudLink.unlink()
        response = self.client.post(reverse("feedback:survey-builder", args=["s"]), {
            "definition_version": 1, "action": "delete-question", "question_uuid": str(self.question.uuid),
        }, follow=True)
        self.assertContains(response, "尚未連結雲端，問卷唯讀")
        self.question.refresh_from_db()
        self.assertTrue(self.question.is_active)

    def test_create_survey_reuses_its_uuid_after_a_lost_response(self):
        form_page = self.client.get(reverse("feedback:survey-create"))
        survey_uuid = form_page.context["pending_survey_uuid"]
        self.assertContains(form_page, f'name="survey_uuid" value="{survey_uuid}"')
        sent = []

        class LostThenOk:
            def post(self, path, body=None):
                sent.append(body["survey_uuid"])
                if len(sent) == 1:  # the cloud created it, but the reply never arrived
                    raise CloudError(TRANSIENT)
                return {"definition": {**body, "version": 1, "slug": "new-survey"}}

        data = {"title": "New Survey", "description": "", "is_active": "on", "analysis_enabled": "on",
                "survey_uuid": survey_uuid}
        with patch("cloudsync.survey_write.client_for_link", return_value=LostThenOk()):
            first = self.client.post(reverse("feedback:survey-create"), data)
            self.assertContains(first, "離線中，問卷唯讀")
            self.assertContains(first, f'name="survey_uuid" value="{survey_uuid}"')  # same uuid on the retry form
            second = self.client.post(reverse("feedback:survey-create"), data)
        self.assertEqual(sent, [survey_uuid, survey_uuid])
        created = Survey.objects.get(slug="new-survey")
        self.assertEqual(str(created.uuid), survey_uuid)
        self.assertRedirects(second, reverse("feedback:survey-builder", args=["new-survey"]), fetch_redirect_response=False)

    def test_create_survey_rejects_a_malformed_uuid(self):
        response = self.client.post(reverse("feedback:survey-create"), {
            "title": "X", "description": "", "is_active": "on", "analysis_enabled": "on", "survey_uuid": "nope"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Survey.objects.filter(title="X").exists())

    def test_category_management_is_cloud_only(self):
        response = self.client.post(reverse("feedback:category-create"), {"name": "新分類"}, follow=True)
        self.assertContains(response, "分類由雲端管理")


class ConnectionPageTests(NodePageCase):
    def test_connect_tests_before_saving(self):
        CloudLink.unlink()
        with patch("cloudsync.views.CloudClient") as client_class:
            client_class.return_value.post.return_value = {"node_uuid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"}
            self.client.post(reverse("cloudsync:connection"), {"action": "connect", "api_url": "https://cloud.example",
                                                                 "token": "fih_new"})
        link = CloudLink.load()
        self.assertEqual((link.api_url, str(link.node_uuid), link.cursor), ("https://cloud.example",
                                                                             "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", ""))
        self.assertEqual(load_token("https://cloud.example"), "fih_new")

    def test_plain_http_url_is_refused(self):
        CloudLink.unlink()
        response = self.client.post(reverse("cloudsync:connection"), {"action": "connect",
                                                                        "api_url": "http://cloud.example", "token": "t"})
        self.assertContains(response, "雲端網址必須使用 https://")
        self.assertFalse(CloudLink.load().is_linked)

    def test_failed_test_saves_nothing(self):
        CloudLink.unlink()
        with patch("cloudsync.views.CloudClient") as client_class:
            client_class.return_value.post.side_effect = CloudError("unauthorized")
            response = self.client.post(reverse("cloudsync:connection"), {"action": "connect",
                                                                            "api_url": "https://cloud.example", "token": "bad"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CloudLink.load().is_linked)
        self.assertIsNone(load_token("https://cloud.example"))

    def test_disconnect_removes_token_and_link(self):
        self.client.post(reverse("cloudsync:connection"), {"action": "disconnect"})
        self.assertFalse(CloudLink.load().is_linked)
        self.assertIsNone(load_token("https://c"))

    def test_admin_is_forbidden(self):
        admin = User.objects.create_user(username="a@x.com", password="x", role=User.Role.MANAGER)
        OrganizationMembership.objects.create(user=admin, organization=Organization.current(), role="admin")
        self.client.force_login(admin)
        self.assertEqual(self.client.get(reverse("cloudsync:connection")).status_code, 403)
