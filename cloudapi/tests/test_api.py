import json

from django.test import TestCase, override_settings

from cloudapi.definition import add_question, serialize_definition
from cloudapi.models import NodeDevice
from cloudapi.writes import create_node_survey
from feedback.models import Question, Survey
from feedback.test_utils import cloud_only

BASE = "/api/node/v1/"
QUESTION = {"title": "Q", "help_text": "", "kind": "short_text", "data_type": "text", "options_text": "",
            "is_required": True, "enable_keyword_tracking": False, "order": 1}


def blank(uuid_text, title="S"):
    from cloudapi.definition import blank_definition

    return blank_definition(uuid_text, title=title)


def blank_v1(uuid_text, title="S"):
    return {key: value for key, value in blank(uuid_text, title).items()
            if key not in ("schema_version", "published", "published_version", "published_at",
                           "analysis_definition_version", "next_question_number")}


@cloud_only
@override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=True)
class NodeApiTests(TestCase):
    def setUp(self):
        self.node, self.token = NodeDevice.issue("office")
        self.other, self.other_token = NodeDevice.issue("other")
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}

    def call(self, method, path, body=None, token=None, **extra):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token or self.token}", **extra}
        kwargs = {"content_type": "application/json", "data": json.dumps(body)} if body is not None else {}
        return getattr(self.client, method)(BASE + path, **kwargs, **headers)

    def test_missing_or_revoked_token_is_401(self):
        self.assertEqual(self.client.get(BASE + "surveys/snapshot/").status_code, 401)
        self.node.status = NodeDevice.Status.REVOKED
        self.node.save()
        self.assertEqual(self.call("get", "surveys/snapshot/").status_code, 401)

    @override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=False)
    def test_disabled_prototype_is_503(self):
        self.assertEqual(self.call("get", "surveys/snapshot/").status_code, 503)

    def test_create_then_snapshot_then_changes(self):
        response = self.call("post", "surveys/", blank("22222222-2222-2222-2222-222222222222"))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.call("post", "surveys/", blank("22222222-2222-2222-2222-222222222222")).status_code, 200)

        snapshot = self.call("get", "surveys/snapshot/").json()
        self.assertEqual([s["survey_uuid"] for s in snapshot["surveys"]], ["22222222-2222-2222-2222-222222222222"])

        changes = self.call("get", f"surveys/changes/?cursor={snapshot['cursor']}").json()
        self.assertEqual(changes["changes"], [])

        survey = Survey.objects.get()
        definition = serialize_definition(survey)
        add_question(definition, QUESTION)
        updated = self.call("put", f"surveys/{survey.uuid}/", {"expected_version": 1, "definition": definition})
        self.assertEqual(updated.status_code, 200)
        changes = self.call("get", f"surveys/changes/?cursor={snapshot['cursor']}").json()
        self.assertEqual([c["definition"]["version"] for c in changes["changes"]], [2])
        self.assertFalse(changes["has_more"])

    def test_put_conflict(self):
        survey = create_node_survey(self.node, blank("33333333-3333-3333-3333-333333333333"))[0].survey
        definition = serialize_definition(survey)
        add_question(definition, {**QUESTION, "kind": "single_choice", "data_type": "nominal", "options_text": "A"})
        self.call("put", f"surveys/{survey.uuid}/", {"expected_version": 1, "definition": definition})
        stale = self.call("put", f"surveys/{survey.uuid}/", {"expected_version": 1, "definition": definition})
        self.assertEqual((stale.status_code, stale.json()["current_version"]), (409, 2))


    def test_invalid_definition_is_400_and_not_stored(self):
        bad = blank("abababab-abab-abab-abab-abababababab")
        bad["questions"] = [{"uuid": "cdcdcdcd-cdcd-cdcd-cdcd-cdcdcdcdcdcd", "code": "", "title": "Q", "help_text": "",
                             "kind": "dropdown", "data_type": "text", "options_text": "", "is_required": True,
                             "enable_keyword_tracking": False, "is_active": True, "order": 1}]
        response = self.call("post", "surveys/", bad)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Survey.objects.filter(uuid="abababab-abab-abab-abab-abababababab").exists())

    def test_snapshot_returns_the_frozen_revision(self):
        revision, _ = create_node_survey(self.node, blank("efefefef-efef-efef-efef-efefefefefef"))
        Question.objects.create(survey=revision.survey, title="未經版本化的直接寫入", kind="short_text", data_type="text")
        snapshot = self.call("get", "surveys/snapshot/").json()
        self.assertEqual(snapshot["surveys"], [revision.definition])

    def test_other_nodes_surveys_are_invisible(self):
        survey = create_node_survey(self.other, blank("44444444-4444-4444-4444-444444444444"))[0].survey
        self.assertEqual(self.call("get", "surveys/snapshot/").json()["surveys"], [])
        self.assertEqual(self.call("get", f"surveys/{survey.uuid}/revisions/1/").status_code, 404)
        response = self.call("put", f"surveys/{survey.uuid}/", {"expected_version": 1, "definition": serialize_definition(survey)})
        self.assertEqual(response.status_code, 404)

    def test_invalid_or_foreign_cursor_is_410(self):
        other_cursor = self.call("get", "surveys/snapshot/", token=self.other_token).json()["cursor"]
        self.assertEqual(self.call("get", f"surveys/changes/?cursor={other_cursor}").status_code, 410)
        self.assertEqual(self.call("get", "surveys/changes/?cursor=garbage").status_code, 410)

    def test_heartbeat_reports_node_and_updates_last_seen(self):
        body = self.call("post", "heartbeat/", {}).json()
        self.assertEqual(body["node_uuid"], str(self.node.uuid))
        self.node.refresh_from_db()
        self.assertIsNotNone(self.node.last_seen_at)


@cloud_only
@override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=True)
class NodeApiLifecycleTests(TestCase):
    def setUp(self):
        self.node, self.token = NodeDevice.issue("office")

    def call(self, method, path, body):
        return getattr(self.client, method)(BASE + path, data=json.dumps(body), content_type="application/json",
                                            HTTP_AUTHORIZATION=f"Bearer {self.token}")

    def test_api_rejects_v1_writes(self):
        response = self.call("post", "surveys/", blank_v1("55555555-5555-5555-5555-555555555555"))
        self.assertEqual((response.status_code, response.json()["error"]), (400, "schema_version"))

    def test_api_returns_published_locked_422(self):
        from cloudapi.definition import blank_definition
        from feedback.test_utils import published

        survey = create_node_survey(self.node, blank_definition("66666666-6666-6666-6666-666666666666"))[0].survey
        definition = serialize_definition(survey)
        add_question(definition, {"title": "Q", "kind": "short_text"})
        self.call("put", f"surveys/{survey.uuid}/", {"expected_version": 1, "definition": definition})
        survey = published(Survey.objects.get(pk=survey.pk))
        definition = serialize_definition(survey)
        definition["title"] = "改名"
        response = self.call("put", f"surveys/{survey.uuid}/",
                             {"expected_version": survey.definition_version, "definition": definition})
        self.assertEqual((response.status_code, response.json()["error"]), (422, "published_locked"))
