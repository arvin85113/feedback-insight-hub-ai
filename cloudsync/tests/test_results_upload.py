from django.test import TestCase

from cloudsync.client import CLIENT, CONFLICT, TRANSIENT, UNAUTHORIZED, CloudError
from cloudsync.definitions import upsert_definition
from cloudsync.models import ResultUpload
from cloudsync.results import record_publication, retry_failed, upload_results
from cloudsync.tests.test_inbox import definition
from cloudsync.tests.test_results_local import published_state


class FakeResultClient:
    def __init__(self, responses):
        self.responses, self.sent = list(responses), []

    def post_raw(self, path, data):
        import json

        assert path == "results/"
        self.sent.append(json.loads(data))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class UploadResultsTests(TestCase):
    def setUp(self):
        survey, _ = upsert_definition(definition(1))
        state = published_state(survey)
        record_publication(state)
        record_publication(published_state(survey))  # a second publication, not a resend

    def test_identity_is_reused_and_statuses_follow_replies(self):
        client = FakeResultClient([CloudError(TRANSIENT), {"status": "applied"}, {"status": "stale"}])
        with self.assertRaises(CloudError):
            upload_results(client)
        first = ResultUpload.objects.order_by("publish_sequence").first()
        self.assertEqual((first.status, first.attempts), ("pending", 1))
        upload_results(client)
        self.assertEqual(list(ResultUpload.objects.order_by("publish_sequence").values_list("status", flat=True)),
                         ["uploaded", "stale"])
        self.assertEqual(client.sent[0]["publish_uuid"], client.sent[1]["publish_uuid"])
        self.assertEqual(client.sent[0]["content_hash"], client.sent[1]["content_hash"])

    def test_unauthorized_stays_pending_and_conflicts_fail_until_retried(self):
        with self.assertRaises(CloudError):
            upload_results(FakeResultClient([CloudError(UNAUTHORIZED)]))
        self.assertEqual(set(ResultUpload.objects.values_list("status", flat=True)), {"pending"})
        upload_results(FakeResultClient([CloudError(CONFLICT), CloudError(CLIENT, status=404)]))
        self.assertEqual(set(ResultUpload.objects.values_list("last_error", flat=True)), {"conflict", "not_found"})
        upload_results(FakeResultClient([]))  # failed items are not retried automatically
        self.assertEqual(retry_failed(), 2)
        self.assertEqual(set(ResultUpload.objects.values_list("status", flat=True)), {"pending"})

    def test_oversized_content_fails_without_sending(self):
        from django.test import override_settings

        with override_settings(CLOUD_RESULT_MAX_BYTES=10):
            client = FakeResultClient([])
            upload_results(client)
        self.assertEqual((client.sent, set(ResultUpload.objects.values_list("last_error", flat=True))), ([], {"too_large"}))
