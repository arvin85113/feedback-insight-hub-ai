import copy
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.urls import reverse

from cloudsync.client import CloudError, TRANSIENT
from cloudsync.models import CloudLink
from feedback.models import AnalysisJob, Survey
from feedback.test_external_dataset import dataset_fixture
from node.audit import DATASET_REGISTERED
from node.models import LocalDatasetLocation, NodeAuditEvent
from node.tests.test_console import ConsoleTestCase, Role


class DatasetPageTests(ConsoleTestCase):
    def setUp(self):
        super().setUp()
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.manifest, self.mapping, _ = dataset_fixture(Path(self.work.name))
        data = json.loads(self.mapping.read_text(encoding="utf-8"))
        data.update(survey={"title": "Fixture reviews", "description": "Test only"},
                    deduplication_fields=["source_identity_sha256"])
        self.mapping.write_text(json.dumps(data), encoding="utf-8")
        self.link = CloudLink.relink("https://fixture.invalid", "dddddddd-dddd-dddd-dddd-dddddddddddd")
        self.url = reverse("node:datasets")
        self.data = {"manifest_path": str(self.manifest), "mapping_path": str(self.mapping), "action": "validate"}
        self.client.force_login(self.owner)

    def preview(self):
        response = self.client.post(self.url, self.data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "確認登錄並安排本機分析")
        return response.context["form"].initial["confirmation"]

    @staticmethod
    def cloud_reply(definition, registration):
        return {"definition": {**copy.deepcopy(definition), "version": 1, "slug": "fixture-external",
            "published": True, "published_version": 1, "analysis_definition_version": 1,
            "published_at": "2026-10-05T00:00:00+00:00", "external_source": registration}}

    def test_owner_only_and_get_does_not_read_dataset_files(self):
        with patch("feedback.external_dataset.file_hash", side_effect=AssertionError("GET must not hash")):
            self.assertEqual(self.client.get(self.url).status_code, 200)
        for role in (Role.ADMIN,):
            member = self._member(f"{role}@fixture.invalid", role)
            self.client.force_login(member)
            self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_preview_is_read_only_and_bad_path_is_safe(self):
        self.preview()
        self.assertFalse(Survey.objects.exists())
        self.assertFalse(LocalDatasetLocation.objects.exists())
        response = self.client.post(self.url, {**self.data, "manifest_path": "missing"})
        self.assertContains(response, "驗證失敗")
        self.assertFalse(Survey.objects.exists())

    def test_local_only_source_resumes_registration_then_recomputes_cloud_version_once(self):
        import uuid
        from cloudapi.definition import apply_definition
        from feedback.analysis_jobs import suppress_analysis_scheduling
        from feedback.external_dataset import validate_external_dataset
        from feedback.importing.mapping import load_mapping
        from feedback.importing.service import mapping_definition
        from feedback.models import SurveyAnalysisState
        from node.datasets import register_local_dataset
        verified = validate_external_dataset(self.manifest, self.mapping)
        identity = uuid.uuid5(uuid.NAMESPACE_URL, "feedback-external:" + ":".join(
            verified.registration[key] for key in ("source_ref", "mapping_key", "schema_sha256")))
        with suppress_analysis_scheduling():
            survey = Survey(uuid=identity)
            apply_definition(survey, mapping_definition(load_mapping(self.mapping), identity), version=0)
            register_local_dataset(survey.pk, verified)
        state, _ = SurveyAnalysisState.objects.get_or_create(survey=survey)
        before = state.config_version
        with patch("feedback.external_dataset.file_hash", side_effect=AssertionError("GET must not hash")):
            response = self.client.get(self.url, {"survey": survey.pk})
            self.assertEqual(response.context["form"].initial["manifest_path"], str(self.manifest))
            self.assertEqual(response.context["form"].initial["mapping_path"], str(self.mapping))
        confirmation = self.preview()

        def post(path, body):
            self.assertEqual(body["expected_version"], 0)
            return self.cloud_reply(body["definition"], body["registration"])

        with patch("cloudsync.datasets._client") as factory:
            factory.return_value.post.side_effect = post
            for _ in range(2):
                self.assertEqual(self.client.post(self.url, {**self.data, "action": "register",
                                                           "confirmation": confirmation}).status_code, 302)
        state.refresh_from_db()
        self.assertEqual(state.config_version, before + 1)
        self.assertEqual(Survey.objects.count(), 1)
        self.assertEqual(LocalDatasetLocation.objects.count(), 1)
        self.assertEqual(AnalysisJob.objects.filter(survey=survey, status="pending", executor="deterministic").count(), 1)

    def test_register_then_retry_keeps_one_survey_and_audits_without_paths(self):
        confirmation = self.preview()

        def post(path, body):
            self.assertEqual(path, "datasets/register/")
            self.assertNotIn(str(self.work.name), json.dumps(body))
            return self.cloud_reply(body["definition"], body["registration"])

        with patch("cloudsync.datasets._client") as factory:
            factory.return_value.post.side_effect = post
            for _ in range(2):
                response = self.client.post(self.url, {**self.data, "action": "register", "confirmation": confirmation})
                self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual((Survey.objects.count(), LocalDatasetLocation.objects.count()), (1, 1))
        self.assertTrue(AnalysisJob.objects.filter(source_kind="external", status="pending").exists())
        event = NodeAuditEvent.objects.filter(action=DATASET_REGISTERED).first()
        self.assertNotIn(str(self.work.name), json.dumps(event.details))
        self.assertContains(self.client.get(self.url), "路徑已登錄")
        self.assertContains(self.client.get(self.url), "最近本機發布")
        from feedback.views import analysis_visible_surveys
        self.assertEqual(list(analysis_visible_surveys()), list(Survey.objects.all()))

    def test_equal_version_local_copy_is_not_silently_treated_as_cloud_bound(self):
        from cloudapi.errors import DefinitionError
        from cloudapi.models import SurveyDefinitionRevision
        from cloudsync.datasets import register_dataset
        from feedback.external_dataset import validate_external_dataset
        from feedback.importing.mapping import load_mapping
        from feedback.importing.service import mapping_definition
        import uuid

        verified = validate_external_dataset(self.manifest, self.mapping)
        identity = uuid.uuid5(uuid.NAMESPACE_URL, "feedback-external:" + ":".join(
            verified.registration[key] for key in ("source_ref", "mapping_key", "schema_sha256")))
        Survey.objects.create(uuid=identity, title="Unverified local definition", slug="local-conflict",
                              definition_version=1)
        definition = mapping_definition(load_mapping(self.mapping), identity)
        with patch("cloudsync.datasets._client") as factory:
            factory.return_value.post.side_effect = lambda path, body: self.cloud_reply(
                body["definition"], body["registration"])
            with self.assertRaises(DefinitionError):
                register_dataset(verified, definition, expected_version=1, generation=self.link.generation)
        self.assertFalse(SurveyDefinitionRevision.objects.exists())
        self.assertFalse(LocalDatasetLocation.objects.exists())
        self.assertEqual(Survey.objects.get().title, "Unverified local definition")

    def test_dataset_list_does_not_reuse_previous_upload_acknowledgement(self):
        from datetime import timedelta
        from cloudsync.models import ResultUpload
        from cloudsync.tests.test_results_local import published_state

        confirmation = self.preview()
        with patch("cloudsync.datasets._client") as factory:
            factory.return_value.post.side_effect = lambda path, body: self.cloud_reply(
                body["definition"], body["registration"])
            self.client.post(self.url, {**self.data, "action": "register", "confirmation": confirmation})
        survey = Survey.objects.get()
        state = published_state(survey)
        ResultUpload.objects.create(survey=survey, published_at=state.published_at - timedelta(minutes=1),
                                   publish_sequence=1, status="uploaded", content_hash="fixture", content={})
        response = self.client.get(self.url)
        self.assertIsNone(list(response.context["sources"])[0].last_upload_status)
        self.assertContains(response, "尚無本次上傳")

    def test_registration_canonicalizes_timezone_and_accepts_equivalent_utc_reply_on_retry(self):
        from datetime import datetime
        from cloudsync.datasets import register_dataset
        from feedback.external_dataset import validate_external_dataset
        from feedback.importing.mapping import load_mapping
        from feedback.importing.service import mapping_definition
        import uuid

        verified = validate_external_dataset(self.manifest, self.mapping)
        verified.registration["source_latest_at"] = datetime.fromisoformat("2012-12-20T00:00:00+08:00")
        definition = mapping_definition(load_mapping(self.mapping), uuid.uuid4())

        def post(path, body):
            self.assertEqual(body["registration"]["source_latest_at"], "2012-12-19T16:00:00+00:00")
            reply = self.cloud_reply(body["definition"], copy.deepcopy(body["registration"]))
            reply["definition"]["external_source"]["source_latest_at"] = "2012-12-19T16:00:00Z"
            return reply

        with patch("cloudsync.datasets._client") as factory:
            factory.return_value.post.side_effect = post
            for _ in range(2):
                register_dataset(verified, definition, expected_version=0, generation=self.link.generation)
        self.assertEqual(Survey.objects.count(), 1)
        self.assertEqual(LocalDatasetLocation.objects.count(), 1)

    def test_registration_rejects_a_genuinely_different_source_time(self):
        from datetime import datetime
        from cloudapi.errors import DefinitionError
        from cloudsync.datasets import register_dataset
        from feedback.external_dataset import validate_external_dataset
        from feedback.importing.mapping import load_mapping
        from feedback.importing.service import mapping_definition
        import uuid

        verified = validate_external_dataset(self.manifest, self.mapping)
        verified.registration["source_latest_at"] = datetime.fromisoformat("2012-12-20T00:00:00+08:00")
        definition = mapping_definition(load_mapping(self.mapping), uuid.uuid4())

        def post(path, body):
            reply = self.cloud_reply(body["definition"], copy.deepcopy(body["registration"]))
            reply["definition"]["external_source"]["source_latest_at"] = "2012-12-20T00:00:00Z"
            return reply

        with patch("cloudsync.datasets._client") as factory:
            factory.return_value.post.side_effect = post
            with self.assertRaises(DefinitionError):
                register_dataset(verified, definition, expected_version=0, generation=self.link.generation)
        self.assertFalse(Survey.objects.exists())
        self.assertFalse(LocalDatasetLocation.objects.exists())

    def test_changed_mapping_or_link_invalidates_confirmation_before_api(self):
        confirmation = self.preview()
        self.mapping.write_text(self.mapping.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with patch("cloudsync.datasets._client") as factory:
            response = self.client.post(self.url, {**self.data, "action": "register", "confirmation": confirmation})
            self.assertContains(response, "驗證失敗")
            factory.assert_not_called()
        confirmation = self.preview()
        CloudLink.unlink()
        with patch("cloudsync.datasets._client") as factory:
            response = self.client.post(self.url, {**self.data, "action": "register", "confirmation": confirmation})
            self.assertContains(response, "驗證失敗")
            factory.assert_not_called()

    def test_timeout_leaves_no_local_write_and_explicit_retry_reuses_uuid(self):
        confirmation = self.preview()
        bodies = []

        def post(path, body):
            bodies.append(body)
            if len(bodies) == 1:
                raise CloudError(TRANSIENT)
            return self.cloud_reply(body["definition"], body["registration"])

        with patch("cloudsync.datasets._client") as factory:
            factory.return_value.post.side_effect = post
            first = self.client.post(self.url, {**self.data, "action": "register", "confirmation": confirmation})
            self.assertContains(first, "尚未完成本機登錄")
            self.assertFalse(Survey.objects.exists())
            second = self.client.post(self.url, {**self.data, "action": "register", "confirmation": confirmation})
            self.assertEqual(second.status_code, 302)
        self.assertEqual(bodies[0]["definition"]["survey_uuid"], bodies[1]["definition"]["survey_uuid"])

    def test_relink_during_http_rolls_back_local_copy(self):
        confirmation = self.preview()

        def post(path, body):
            CloudLink.unlink()
            return self.cloud_reply(body["definition"], body["registration"])

        with patch("cloudsync.datasets._client") as factory:
            factory.return_value.post.side_effect = post
            self.assertContains(self.client.post(self.url, {**self.data, "action": "register", "confirmation": confirmation}),
                                "雲端連結已變更")
        self.assertFalse(Survey.objects.exists())

    def test_reply_metadata_mismatch_leaves_no_local_write(self):
        confirmation = self.preview()

        def post(path, body):
            return self.cloud_reply(body["definition"], {**body["registration"], "row_count": 999})

        with patch("cloudsync.datasets._client") as factory:
            factory.return_value.post.side_effect = post
            self.assertContains(self.client.post(self.url, {**self.data, "action": "register", "confirmation": confirmation}),
                                "尚未完成本機登錄")
        self.assertFalse(LocalDatasetLocation.objects.exists())

    def test_attaching_files_recovers_job_failed_before_locator_was_registered(self):
        from cloudsync.definitions import upsert_definition

        confirmation = self.preview()

        def post(path, body):
            reply = self.cloud_reply(body["definition"], body["registration"])
            # A background definition sync arrived before the OWNER attached files.
            survey, _ = upsert_definition(reply["definition"])
            AnalysisJob.objects.filter(survey=survey, source_kind="external").update(
                status="failed", error_code="external_location_missing")
            return reply

        with patch("cloudsync.datasets._client") as factory:
            factory.return_value.post.side_effect = post
            response = self.client.post(self.url, {**self.data, "action": "register", "confirmation": confirmation})
            self.assertEqual(response.status_code, 302)
        self.assertEqual(AnalysisJob.objects.filter(source_kind="external", status="pending").count(), 1)
        self.assertTrue(AnalysisJob.objects.filter(error_code="external_location_missing", status="failed").exists())
