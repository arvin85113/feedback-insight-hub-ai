import uuid
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import TestCase, override_settings

from cloudapi.envelope import canonical_bytes, sha256_hex
from cloudapi.freshness import node_freshness
from cloudapi.models import NodeDevice, PublishedResultRecord
from cloudapi.results import ResultInvalid, apply_upload
from cloudapi.tests.test_results import content
from cloudapi.writes import assign_survey_to_node
from feedback.analysis_sources import external_version_identity, register_external_dataset_version
from feedback.models import Survey, SurveyAnalysisState
from feedback.test_utils import cloud_only


@cloud_only
class ExternalResultTests(TestCase):
    def setUp(self):
        self.node, self.token = NodeDevice.issue("fixture")
        self.survey = assign_survey_to_node(Survey.objects.create(title="External", slug="external"), self.node).survey
        self.first = self.register("a")

    def register(self, marker):
        return register_external_dataset_version(
            self.survey.pk, source_ref="fixture/reviews", source_version=f"v-{marker}",
            source_revision=f"revision-{marker}", cleaning_version="clean-v1", content_sha256=marker * 64,
            schema_sha256="f" * 64, mapping_key="fixture", mapping_version="v1", row_count=4,
        )[1]

    def body(self, version):
        return {**content(self.survey, watermark=0), "input_source": external_version_identity(version)}

    def upload(self, sequence, body, upload_uuid=None):
        return apply_upload(self.node, publish_uuid=upload_uuid or uuid.uuid4(), publish_sequence=sequence,
                            content_hash=sha256_hex(body), content=body)

    def test_external_upload_is_latest_and_idempotent(self):
        upload_uuid = uuid.uuid4()
        body = self.body(self.first)
        self.assertEqual(self.upload(1, body, upload_uuid), ("applied", True))
        self.assertEqual(self.upload(1, body, upload_uuid), ("applied", False))
        state = SurveyAnalysisState.objects.get(survey=self.survey)
        self.assertTrue(node_freshness(self.survey, state)["source_current"])
        self.assertTrue(node_freshness(self.survey, state)["is_latest"])
        self.assertEqual(PublishedResultRecord.objects.count(), 1)

    def test_source_change_marks_last_success_old_and_old_worker_cannot_replace_it(self):
        self.upload(1, self.body(self.first))
        state = SurveyAnalysisState.objects.get(survey=self.survey)
        second = self.register("b")
        self.survey.refresh_from_db()
        freshness = node_freshness(self.survey, state)
        self.assertFalse(freshness["is_latest"])
        html = render_to_string("feedback/_analysis_publication_status.html",
                                {"analysis_publication": {"node_result": freshness}})
        self.assertIn("外部資料版本已變更，仍顯示上一版成功結果", html)
        self.assertEqual(self.upload(2, self.body(self.first)), ("stale", True))
        self.assertEqual(self.upload(3, self.body(second)), ("applied", True))
        self.assertEqual(self.upload(4, self.body(self.first)), ("stale", True))
        state.refresh_from_db()
        self.assertEqual(state.publish_sequence, 3)
        self.assertEqual(state.publication_manifest["input_source"], external_version_identity(second))
        self.assertEqual(PublishedResultRecord.objects.count(), 4)

    def test_wrong_hash_unknown_version_local_path_and_watermark_are_rejected(self):
        good = self.body(self.first)
        for bad in (
            {**good, "input_source": {**good["input_source"], "content_sha256": "0" * 64}},
            {**good, "input_source": {**good["input_source"], "source_version": "unknown"}},
            {**good, "input_source": {**good["input_source"], "manifest_path": "private"}},
            {**good, "analyzed_through_sequence": 1},
            {**good, "input_source": {"kind": "answers"}},
        ):
            with self.assertRaises(ResultInvalid):
                self.upload(1, bad)
        self.assertFalse(PublishedResultRecord.objects.exists())

    @override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=True)
    def test_external_result_api_accepts_once_and_resend_is_idempotent(self):
        body = self.body(self.first)
        request_body = canonical_bytes({
            "publish_uuid": str(uuid.uuid4()), "publish_sequence": 1,
            "content_hash": sha256_hex(body), "content": body,
        })
        responses = [self.client.post(
            "/api/node/v1/results/", data=request_body, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        ) for _ in range(2)]
        self.assertEqual([response.status_code for response in responses], [201, 200])
        self.assertEqual([response.json()["status"] for response in responses], ["applied", "applied"])
        self.assertEqual(PublishedResultRecord.objects.count(), 1)

    def test_ownership_is_rechecked_after_validation_before_any_history_write(self):
        from cloudapi import results

        other, _ = NodeDevice.issue("other-fixture")
        original = results._validate

        def validated_then_reassigned(*args, **kwargs):
            validated = original(*args, **kwargs)
            Survey.objects.filter(pk=self.survey.pk).update(owner_node=other)
            return validated

        with patch.object(results, "_validate", side_effect=validated_then_reassigned):
            with self.assertRaises(PermissionError):
                self.upload(1, self.body(self.first))
        self.assertFalse(PublishedResultRecord.objects.exists())
