import json
import uuid

from django.test import TestCase, override_settings

from cloudapi.envelope import canonical_bytes, sha256_hex
from cloudapi.models import NodeDevice, PublishedResultRecord
from cloudapi.results import ResultConflict, ResultInvalid, apply_upload
from cloudapi.writes import assign_survey_to_node
from feedback.models import Survey, SurveyAnalysisState
from feedback.test_utils import cloud_only

STAGE = {"current": True, "input_version": 1, "config_version": 1, "pipeline_version": "p"}


def content(survey, *, watermark=1, version=1, title="v"):
    return {"survey_uuid": str(survey.uuid), "definition_version": version, "analyzed_through_sequence": watermark,
            "input_fingerprint": "f", "published_at": "2026-10-03T00:00:00+00:00",
            "pipeline": {"input_version": 1, "config_version": 1, "pipeline_version": "p", "implementation_version": "i"},
            "stages": {"statistics": STAGE, "text": STAGE, "ai": {**STAGE, "current": False, "model_name": None}},
            "display_payload": {"statistics": {"title": title}, "text_analysis": {}}, "ai_payload": None,
            "ai_source": {}, "coverage": {"analyzed_unique": 3, "excluded": {"voided": 0, "incomplete": 0}}}


@cloud_only
class ApplyUploadTests(TestCase):
    def setUp(self):
        self.node, self.token = NodeDevice.issue("office")
        self.survey = assign_survey_to_node(Survey.objects.create(title="S", slug="s"), self.node).survey
        Survey.objects.filter(pk=self.survey.pk).update(response_sequence=5)

    def upload(self, sequence, body, publish_uuid=None):
        return apply_upload(self.node, publish_uuid=str(publish_uuid or uuid.uuid4()), publish_sequence=sequence,
                            content_hash=sha256_hex(body), content=body)

    def test_newer_upload_switches_the_display(self):
        self.assertEqual(self.upload(1, content(self.survey, title="first")), ("applied", True))
        state = SurveyAnalysisState.objects.get(survey=self.survey)
        self.assertEqual((state.publish_sequence, state.published_display_payload["statistics"]["title"]), (1, "first"))
        self.assertIsNone(state.published_snapshot_id)

    def test_stale_upload_keeps_history_only(self):
        self.upload(5, content(self.survey, title="five"))
        for sequence, body in ((4, content(self.survey, title="older")),
                               (6, content(self.survey, title="lower watermark", watermark=0)),
                               (7, content(self.survey, title="older definition", version=0))):
            with self.subTest(sequence):
                self.assertEqual(self.upload(sequence, body)[0], "stale")
        self.assertEqual(SurveyAnalysisState.objects.get(survey=self.survey).published_display_payload["statistics"]["title"], "five")
        self.assertEqual(PublishedResultRecord.objects.count(), 4)

    def test_resend_is_idempotent_and_mismatch_conflicts(self):
        publish_uuid = uuid.uuid4()
        body = content(self.survey)
        self.upload(1, body, publish_uuid)
        self.assertEqual(self.upload(1, body, publish_uuid), ("applied", False))
        self.upload(2, content(self.survey, title="newer"))
        self.assertEqual(self.upload(1, body, publish_uuid), ("stale", False))  # replaced since: answer with today's relation
        for sequence, other in ((1, content(self.survey, title="other")), (2, body)):
            with self.subTest(sequence), self.assertRaises(ResultConflict):
                self.upload(sequence, other, publish_uuid)
        self.assertEqual(PublishedResultRecord.objects.get(publish_uuid=publish_uuid).conflict_count, 2)

    def test_impossible_values_are_rejected(self):
        for body in (content(self.survey, watermark=6), content(self.survey, version=2),
                     {**content(self.survey), "analyzed_through_sequence": True},
                     {**content(self.survey), "survey_uuid": "not-a-uuid"}):
            with self.subTest(body.get("analyzed_through_sequence")), self.assertRaises(ResultInvalid):
                self.upload(1, body)

    @override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=True)
    def test_api_status_codes_utf8_body_and_heartbeat_sequence(self):
        def post(body):
            return self.client.post("/api/node/v1/results/", data=canonical_bytes(body),
                                    content_type="application/json; charset=utf-8",
                                    HTTP_AUTHORIZATION=f"Bearer {self.token}")
        body = content(self.survey, title="中文標題")
        good = {"publish_uuid": str(uuid.uuid4()), "publish_sequence": 1, "content_hash": sha256_hex(body), "content": body}
        self.assertEqual((post(good).status_code, post(good).status_code), (201, 200))
        self.assertEqual(post({**good, "publish_uuid": str(uuid.uuid4()), "content_hash": "0" * 64}).status_code, 400)
        with override_settings(CLOUD_RESULT_MAX_BYTES=100):
            self.assertEqual(post({**good, "publish_uuid": str(uuid.uuid4())}).status_code, 413)
        beat = self.client.post("/api/node/v1/heartbeat/", data="{}", content_type="application/json",
                                HTTP_AUTHORIZATION=f"Bearer {self.token}").json()
        self.assertEqual(beat["surveys"][0]["publish_sequence"], 1)
