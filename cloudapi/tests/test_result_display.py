import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from cloudapi.envelope import sha256_hex
from cloudapi.models import NodeDevice, SubmissionReceipt
from cloudapi.results import apply_upload
from cloudapi.tests.test_results import content
from cloudapi.writes import assign_survey_to_node
from feedback.models import FeedbackSubmission, Survey
from feedback.published_analysis import get_published_analysis_payload
from feedback.test_utils import cloud_only


def make_receipt(survey, sequence, status="received"):
    return SubmissionReceipt.objects.create(
        submission_uuid=uuid.uuid4(), node=survey.owner_node, survey=survey, submitted_at=timezone.now(),
        definition_version=1, response_sequence=sequence, payload_hash="0" * 64, status=status)


@cloud_only
class ResultDisplayTests(TestCase):
    def setUp(self):
        self.node, _ = NodeDevice.issue("office")
        self.survey = assign_survey_to_node(Survey.objects.create(title="S", slug="s"), self.node).survey
        Survey.objects.filter(pk=self.survey.pk).update(response_sequence=1)
        make_receipt(self.survey, 1, status="synced")
        body = content(self.survey)
        apply_upload(self.node, publish_uuid=str(uuid.uuid4()), publish_sequence=1,
                     content_hash=sha256_hex(body), content=body)
        manager = get_user_model().objects.create_user(username="m", password="x", role="manager")
        self.client.force_login(manager)

    def payload(self):
        return get_published_analysis_payload(Survey.objects.get(pk=self.survey.pk))

    def test_uploaded_result_is_available_and_latest(self):
        payload = self.payload()
        self.assertTrue(payload["available"])
        self.assertTrue(payload["node_result"]["is_latest"])
        self.assertEqual((payload["freshness"]["statistics"], payload["freshness"]["ai"]), (True, False))

    def test_new_quarantined_and_abandoned_replies(self):
        make_receipt(self.survey, 2, status="quarantined")
        make_receipt(self.survey, 3, status="abandoned")
        result = self.payload()["node_result"]
        self.assertEqual((result["pending_new"], result["is_latest"]), (1, False))
        Survey.objects.filter(pk=self.survey.pk).update(definition_version=9)
        self.assertFalse(self.payload()["node_result"]["definition_current"])

    def test_assigned_survey_without_upload_is_not_latest(self):
        from feedback.models import SurveyAnalysisState

        other = assign_survey_to_node(Survey.objects.create(title="O", slug="o"), self.node).survey
        result = get_published_analysis_payload(other)["node_result"]
        self.assertEqual((result["has_result"], result["is_latest"]), (False, False))
        SurveyAnalysisState.objects.filter(survey=other).delete()  # the early-return branch
        self.assertIn("node_result", get_published_analysis_payload(other))

    def test_page_shows_reasons_and_counts(self):
        make_receipt(self.survey, 2)
        FeedbackSubmission.objects.create(survey=self.survey)
        page = self.client.get(reverse("feedback:stats-overview") + "?survey=s")
        for text in ("本機發布 #1", "有 1 筆新回覆尚未分析", "管線版本由本機申報"):
            self.assertContains(page, text)
        self.assertNotContains(page, "雲端既有")  # no migration of existing replies (cloud sync spec §10)

    def test_catalog_counts_use_uploaded_coverage(self):
        from feedback.views import _survey_catalog_rows

        row = _survey_catalog_rows(Survey.objects.filter(pk=self.survey.pk))[0]
        self.assertEqual(row.response_count, 3)  # coverage.analyzed_unique from the upload

    def test_unassigned_surveys_keep_the_old_payload(self):
        self.assertNotIn("node_result", get_published_analysis_payload(Survey.objects.create(title="P", slug="p")))
