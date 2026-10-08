import uuid
from unittest.mock import patch
from django.test import TestCase

from cloudsync.definitions import upsert_definition
from cloudsync.models import ResultUpload, SurveySyncState
from cloudsync.results import backfill_publications, build_content
from cloudsync.tests.test_inbox import definition
from cloudsync.tests.test_results_local import published_state
from feedback.analysis_sources import AnalysisSourceConfigurationError
from feedback.models import Survey, SurveyAnalysisSource
from node.models import NodeAuditEvent


class ConfigurationIsolationTests(TestCase):
    def test_broken_publication_rolls_back_own_sequence_and_does_not_block_good(self):
        bad, _ = upsert_definition(definition(1))
        other = definition(1)
        other["survey_uuid"], other["slug"] = str(uuid.uuid4()), "good-fixture"
        for question in other["questions"]:
            question["uuid"] = str(uuid.uuid4())
        good, _ = upsert_definition(other)
        published_state(bad)
        published_state(good)

        def content(state):
            if state.survey_id == bad.pk:
                raise AnalysisSourceConfigurationError("private local path must not leak")
            return build_content(state)

        with patch("cloudsync.results.build_content", side_effect=content):
            self.assertEqual(backfill_publications(), 1)
            self.assertEqual(backfill_publications(), 0)
        self.assertTrue(ResultUpload.objects.filter(survey=good).exists())
        self.assertFalse(ResultUpload.objects.filter(survey=bad).exists())
        self.assertFalse(SurveySyncState.objects.filter(survey=bad, local_publish_sequence__gt=0).exists())
        self.assertEqual(NodeAuditEvent.objects.filter(action="result.backfill_failed").count(), 1)
        self.assertNotIn("private local", repr(list(NodeAuditEvent.objects.values())))

    def test_external_missing_version_is_not_accepting_responses(self):
        survey = Survey.objects.create(title="broken", slug="broken", published_version=1)
        SurveyAnalysisSource.objects.create(survey=survey, kind="external")
        survey = Survey.objects.get(pk=survey.pk)
        self.assertFalse(survey.accepts_responses)
