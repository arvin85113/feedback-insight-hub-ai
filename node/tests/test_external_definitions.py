import copy

from django.test import TestCase

from cloudapi.tests.test_external_registration import external_definition, source_metadata
from cloudsync.definitions import upsert_definition
from feedback.models import AnalysisJob, Survey
from node.models import LocalDatasetLocation


class ExternalDefinitionSyncTests(TestCase):
    def setUp(self):
        self.definition = {**external_definition(), "version": 1, "slug": "external-copy",
            "published": True, "published_version": 1, "analysis_definition_version": 1,
            "published_at": "2026-10-05T00:00:00+00:00", "external_source": source_metadata()}

    def test_source_change_syncs_metadata_and_old_definition_cannot_reactivate(self):
        survey, changed = upsert_definition(self.definition)
        self.assertTrue(changed)
        self.assertEqual(survey.analysis_source.active_external_version.source_version, "v-a")
        self.assertFalse(LocalDatasetLocation.objects.exists())
        newer = {**self.definition, "version": 2, "analysis_definition_version": 2,
                 "external_source": source_metadata("b")}
        upsert_definition(newer)
        _, changed = upsert_definition(self.definition)
        self.assertFalse(changed)
        survey = Survey.objects.get(pk=survey.pk)
        self.assertEqual(survey.analysis_source.active_external_version.source_version, "v-b")
        self.assertTrue(AnalysisJob.objects.filter(survey=survey, source_kind="external", source_version="v-b",
                                                 status="pending").exists())

    def test_missing_source_in_newer_definition_rolls_back_instead_of_reverting_to_answers(self):
        survey, _ = upsert_definition(self.definition)
        malformed = copy.deepcopy(self.definition)
        malformed.pop("external_source")
        malformed["version"] = 2
        with self.assertRaises(ValueError):
            upsert_definition(malformed)
        survey.refresh_from_db()
        self.assertEqual(survey.definition_version, 1)
        self.assertEqual(survey.analysis_source.kind, "external")
