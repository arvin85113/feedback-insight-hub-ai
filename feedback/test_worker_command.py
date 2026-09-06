import io
import json
import tempfile
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from .analysis_jobs import schedule_survey_analysis, suppress_analysis_scheduling
from .models import AnalysisJob, Answer, FeedbackSubmission, Question, Survey


class ContinuousWorkerCommandTests(TestCase):
    def setUp(self):
        self.survey = Survey.objects.create(title="Loop worker fixture", slug="loop-worker-fixture")
        with suppress_analysis_scheduling():
            rating = Question.objects.create(
                survey=self.survey,
                title="Rating",
                kind=Question.Kind.SCALE,
                data_type=Question.DataType.ORDINAL,
                options_text="1\n2\n3\n4\n5",
            )
            submission = FeedbackSubmission.objects.create(survey=self.survey)
            Answer.objects.create(submission=submission, question=rating, value="5")

    def test_once_processes_one_deterministic_job_without_ai(self):
        schedule_survey_analysis(self.survey.pk, change="input")
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, patch(
            "feedback.ai_report_service.create_gemini_client",
            side_effect=AssertionError("Gemini must not be called"),
        ):
            call_command(
                "run_analysis_worker",
                worker_id="loop-worker",
                output=directory,
                once=True,
                stdout=output,
            )
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(events[0], {"status": "started", "paid_ai": False})
        self.assertEqual(events[-1]["status"], "published")
        self.assertEqual(AnalysisJob.objects.get().status, AnalysisJob.Status.SUCCEEDED)

    def test_once_without_paid_flag_leaves_ai_job_pending(self):
        schedule_survey_analysis(
            self.survey.pk,
            change="none",
            requested_stages=("synthesis",),
        )
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            call_command(
                "run_analysis_worker",
                worker_id="loop-worker",
                output=directory,
                once=True,
                stdout=output,
            )
        self.assertEqual(AnalysisJob.objects.get().status, AnalysisJob.Status.PENDING)
        self.assertIn('"status": "idle"', output.getvalue())
