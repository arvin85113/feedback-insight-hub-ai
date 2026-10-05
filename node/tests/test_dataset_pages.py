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
