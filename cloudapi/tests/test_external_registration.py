import copy
import json
import uuid

from django.test import TestCase, override_settings

from cloudapi.definition import add_question, blank_definition, serialize_definition
from cloudapi.models import NodeDevice, SurveyChange, SurveyDefinitionRevision
from feedback.models import AnalysisJob, ExternalDatasetVersion, Survey
from feedback.test_utils import cloud_only


def source_metadata(marker="a"):
    return {"source_ref": "fixture/reviews", "source_version": f"v-{marker}",
            "source_revision": f"revision-{marker}", "cleaning_version": "clean-v1",
            "content_sha256": marker * 64, "schema_sha256": "f" * 64,
            "mapping_key": "fixture", "mapping_version": "v1", "row_count": 4,
            "source_latest_at": None, "provenance": {}}


def external_definition():
    definition = blank_definition(uuid.uuid4(), title="External fixture", is_active=False,
                                  thank_you_email_enabled=False, improvement_tracking_enabled=False)
    add_question(definition, {"title": "Overall", "kind": "scale", "data_type": "ordinal",
                              "options_text": "1\n2\n3\n4\n5"})
    return definition


@cloud_only
@override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=True, CLOUD_INBOX_ENABLED=False)
class ExternalRegistrationTests(TestCase):
    def setUp(self):
        self.device, self.token = NodeDevice.issue("fixture")
        self.definition = external_definition()

    def register(self, metadata=None, expected=0, token=None, definition=None):
        return self.client.post("/api/node/v1/datasets/register/", content_type="application/json",
            data=json.dumps({"definition": definition or self.definition,
                             "registration": metadata or source_metadata(), "expected_version": expected}),
            HTTP_AUTHORIZATION=f"Bearer {token or self.token}")

    def test_create_freezes_metadata_without_inbox_or_cloud_job(self):
        response = self.register()
        self.assertEqual(response.status_code, 201, response.content)
        survey = Survey.objects.get()
        self.assertTrue(survey.is_published)
        self.assertEqual(survey.owner_node, self.device)
        self.assertIsNone(survey.inbox_since)
        self.assertFalse(survey.accepts_responses)
        Survey.objects.filter(pk=survey.pk).update(is_active=True)
        self.assertFalse(Survey.objects.get(pk=survey.pk).accepts_responses)
        self.assertFalse(AnalysisJob.objects.exists())
        frozen = response.json()["definition"]
        self.assertEqual(frozen["external_source"], source_metadata())
        self.assertEqual(SurveyDefinitionRevision.objects.get().definition, frozen)
        snapshot = self.client.get("/api/node/v1/surveys/snapshot/",
                                  HTTP_AUTHORIZATION=f"Bearer {self.token}").json()
        self.assertEqual(snapshot["surveys"], [frozen])

    def test_retry_with_old_expected_version_is_noop(self):
        self.assertEqual(self.register().status_code, 201)
        self.assertEqual(self.register().status_code, 200)
        self.assertEqual((Survey.objects.count(), ExternalDatasetVersion.objects.count(),
                          SurveyChange.objects.count(), SurveyDefinitionRevision.objects.count()), (1, 1, 1, 1))

    def test_version_change_is_atomic_and_stale_retry_cannot_reactivate_old_source(self):
        self.register()
        self.assertEqual(self.register(source_metadata("b"), expected=1).status_code, 200)
        survey = Survey.objects.get()
        self.assertEqual((survey.definition_version, survey.analysis_definition_version), (2, 2))
        self.assertEqual(serialize_definition(survey)["external_source"], source_metadata("b"))
        self.assertEqual(self.register(expected=0).status_code, 409)
        self.assertEqual(Survey.objects.get().analysis_source.active_external_version.source_version, "v-b")
        self.assertEqual(ExternalDatasetVersion.objects.count(), 2)

    def test_invalid_metadata_and_paths_do_not_leave_half_created_survey(self):
        for bad in ({**source_metadata(), "manifest_path": "private"},
                    {**source_metadata(), "row_count": True},
                    {**source_metadata(), "content_sha256": "invalid"},
                    {**source_metadata(), "provenance": {"user_id": "private"}},
                    {**source_metadata(), "source_ref": "C:/private"},
                    {**source_metadata(), "source_ref": "../.."},
                    {**source_metadata(), "provenance": {"dataset_url": "https://[invalid"}}):
            self.assertEqual(self.register(bad).status_code, 400)
        self.assertFalse(Survey.objects.exists())
        self.assertFalse(SurveyChange.objects.exists())

    def test_same_version_changed_hash_is_rejected_and_rolled_back(self):
        self.register()
        bad = {**source_metadata(), "content_sha256": "b" * 64}
        self.assertEqual(self.register(bad, expected=1).status_code, 400)
        self.assertEqual((Survey.objects.get().definition_version, SurveyDefinitionRevision.objects.count()), (1, 1))
        self.assertEqual(ExternalDatasetVersion.objects.get().content_sha256, "a" * 64)

    def test_mapping_change_requires_separate_survey(self):
        self.register()
        changed = copy.deepcopy(self.definition)
        changed["questions"][0]["title"] = "Another meaning"
        self.assertEqual(self.register(source_metadata("b"), expected=1, definition=changed).status_code, 400)
        self.assertEqual(Survey.objects.get().definition_version, 1)

    def test_wrong_owner_and_disabled_or_revoked_access(self):
        self.register()
        other, token = NodeDevice.issue("other")
        self.assertEqual(self.register(token=token).status_code, 404)
        self.device.status = NodeDevice.Status.REVOKED
        self.device.save(update_fields=["status"])
        self.assertEqual(self.register().status_code, 401)
        with override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=False):
            self.assertEqual(self.register().status_code, 503)

    def test_regular_api_cannot_forge_or_remove_external_source(self):
        forged = {**self.definition, "external_source": source_metadata()}
        response = self.client.post("/api/node/v1/surveys/", data=json.dumps(forged),
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(response.status_code, 400)
        registered = self.register().json()["definition"]
        registered.pop("external_source")
        response = self.client.put(f"/api/node/v1/surveys/{registered['survey_uuid']}/",
            data=json.dumps({"expected_version": 1, "definition": registered}),
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Survey.objects.get().definition_version, 1)

    def test_normal_answers_survey_is_not_reclassified(self):
        self.client.post("/api/node/v1/surveys/", data=json.dumps(self.definition),
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(self.register(expected=1).status_code, 400)
        self.assertFalse(ExternalDatasetVersion.objects.exists())
