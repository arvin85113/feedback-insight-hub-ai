import tempfile
from unittest.mock import patch

from django.test import TestCase, override_settings

from .analysis_jobs import claim_next_job, suppress_analysis_scheduling
from .analysis_worker import execute_deterministic_job
from .local_service import submit_survey_payload
from .models import AnalysisJob, Answer, Question, Survey, SurveyAnalysisState
from .published_analysis import get_published_analysis_payload


@override_settings(ANALYSIS_AUTO_AI_ENABLED=True, ANALYSIS_READ_PUBLISHED_ONLY=True)
class ScheduledAnalysisEndToEndTests(TestCase):
    def test_new_responses_coalesce_run_and_publish_without_sync_ai(self):
        survey = Survey.objects.create(title="端到端測試", slug="scheduled-e2e")
        with suppress_analysis_scheduling():
            rating = Question.objects.create(
                survey=survey,
                title="整體評分",
                kind=Question.Kind.SCALE,
                data_type=Question.DataType.ORDINAL,
                options_text="1\n2\n3\n4\n5",
                order=1,
            )
            comment = Question.objects.create(
                survey=survey,
                title="評論",
                kind=Question.Kind.LONG_TEXT,
                data_type=Question.DataType.TEXT,
                enable_keyword_tracking=True,
                order=2,
            )

        for index, text in enumerate(("clean friendly room", "not good", "comfortable clean room"), 1):
            submit_survey_payload(
                survey,
                user=None,
                respondent_name="",
                respondent_email="",
                consent_follow_up=False,
                answers={f"question_{rating.pk}": str(index + 2), f"question_{comment.pk}": text},
            )

        self.assertEqual(
            AnalysisJob.objects.filter(
                executor=AnalysisJob.Executor.DETERMINISTIC,
                status=AnalysisJob.Status.PENDING,
            ).count(),
            1,
        )
        state = SurveyAnalysisState.objects.get(survey=survey)
        self.assertEqual(state.input_version, 3)
        self.assertFalse(
            Answer.objects.exclude(analysis_text__isnull=True).exists(),
            "Render submission path must not run text analysis",
        )

        job = claim_next_job(
            "e2e-worker",
            lease_seconds=60,
            executor=AnalysisJob.Executor.DETERMINISTIC,
            external_source_refs=(),
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "feedback.ai_report_service.create_gemini_client",
            side_effect=AssertionError("deterministic worker must not call Gemini"),
        ):
            result = execute_deterministic_job(job, output_root=directory, lease_seconds=60)

        self.assertTrue(result.published)
        publication = get_published_analysis_payload(survey)
        self.assertTrue(publication["available"])
        self.assertTrue(publication["freshness"]["statistics"])
        self.assertTrue(publication["freshness"]["text"])
        self.assertFalse(publication["freshness"]["ai"])
        self.assertTrue(publication["statistics"]["charts"])
        self.assertTrue(publication["text_analysis"]["summary"])
        self.assertIsNone(publication["ai"])
        self.assertEqual(
            AnalysisJob.objects.filter(
                executor=AnalysisJob.Executor.AI,
                status=AnalysisJob.Status.PENDING,
            ).count(),
            1,
        )
