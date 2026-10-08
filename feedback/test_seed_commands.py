import tempfile
from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase

from feedback.analysis_jobs import _is_mock_stage, claim_next_job
from feedback.analysis_worker import execute_deterministic_job
from feedback.local_service import build_stats_payload
from feedback.management.commands.seed_demo_beverage import NAME_PREFIX, SURVEY_SLUG
from feedback.models import AnalysisJob, Answer, FeedbackSubmission, Survey, SurveyAIAnalysisStage
from feedback.published_analysis import get_published_analysis_payload
from feedback.test_utils import cloud_only

SEVEN = {"welch_t_test", "one_way_anova", "chi_square", "mann_whitney_u", "kruskal_wallis", "pearson", "spearman"}


def seed(*args):
    call_command("seed_demo_beverage", "--seed", "7", *args, stdout=StringIO())
    return Survey.objects.get(slug=SURVEY_SLUG)


class BeverageSeedTests(TestCase):
    def test_beverage_seed_produces_all_seven_methods(self):
        survey = seed()
        self.assertIsNotNone(survey.published_version)
        payload = build_stats_payload(survey)
        produced = {r["method_key"] for r in payload["inferential_analysis"] if not r.get("skipped_reason")}
        self.assertEqual(SEVEN - produced, set())

    def test_beverage_seed_question_set(self):
        survey = seed()
        kinds = [(q.title, q.kind) for q in survey.questions.order_by("order")]
        self.assertEqual([kind for _title, kind in kinds],
                         ["single_choice", "single_choice", "multiple_choice", "scale", "scale", "single_choice",
                          "decimal", "decimal", "integer", "long_text"])
        wait = survey.questions.get(title="等候時間感受")
        self.assertEqual(wait.analysis_excluded_options, ["不適用"])
        recommend = survey.questions.get(title="推薦意願")
        self.assertEqual((recommend.scale_min, recommend.scale_max), (0, 10))

    def test_beverage_seed_marks_simulated_respondents(self):
        seed()
        names = FeedbackSubmission.objects.values_list("respondent_name", flat=True)
        self.assertEqual(len(names), 100)
        self.assertTrue(all(name.startswith(NAME_PREFIX) for name in names))
        self.assertTrue(Answer.objects.filter(question__kind="multiple_choice").exclude(choice_codes=None).exists())

    def test_beverage_reset_twice(self):
        seed()
        seed("--reset", "--yes")
        survey = seed("--reset", "--yes")
        self.assertEqual(Survey.objects.filter(slug=SURVEY_SLUG).count(), 1)
        self.assertEqual(survey.submissions.count(), 100)

    def test_seed_refuses_existing_without_reset(self):
        seed()
        with self.assertRaisesMessage(CommandError, "--reset"):
            seed()

    def test_beverage_seed_text_analysis_and_publish(self):
        survey = seed()
        job = claim_next_job("seed-worker", lease_seconds=60, executor=AnalysisJob.Executor.DETERMINISTIC,
                             external_source_refs=())
        with tempfile.TemporaryDirectory() as directory, patch(
            "feedback.ai_report_service.create_gemini_client",
            side_effect=AssertionError("deterministic worker must not call Gemini"),
        ):
            self.assertTrue(execute_deterministic_job(job, output_root=directory, lease_seconds=60).published)
        publication = get_published_analysis_payload(survey)
        self.assertTrue(publication["freshness"]["statistics"])
        self.assertTrue(publication["freshness"]["text"])
        self.assertTrue(publication["text_analysis"]["keywords"])

    def test_analysis_mock_mode_still_cannot_publish(self):
        self.assertTrue(_is_mock_stage(SurveyAIAnalysisStage(model_name="mock", output_json={})))


class OtherSeedTests(TestCase):
    def test_other_seeds_create_published_surveys_with_codes(self):
        call_command("seed_demo", stdout=StringIO())
        call_command("seed_notification_test", stdout=StringIO())
        for survey in Survey.objects.all():
            self.assertIsNotNone(survey.published_version, survey.slug)
            for question in survey.questions.filter(kind__in=("single_choice", "multiple_choice")):
                self.assertTrue(all(choice["code"] for choice in question.choices), question.title)

    def test_random_responses_fill_existing_published_survey_with_codes_and_version(self):
        call_command("seed_demo", stdout=StringIO())
        survey = Survey.objects.get(slug="product-feedback")
        call_command("seed_random_responses", "product-feedback", "--count", "5", "--seed", "3",
                     stdout=StringIO())
        submissions = FeedbackSubmission.objects.filter(survey=survey)
        self.assertEqual(submissions.count(), 5)
        self.assertEqual(set(submissions.values_list("definition_version", flat=True)), {survey.published_version})
        self.assertTrue(Answer.objects.filter(submission__survey=survey, question__kind="single_choice")
                        .exclude(choice_codes=None).exists())


class BeverageNodePathTests(TestCase):
    def test_default_path_answers_unchanged(self):
        import random

        from feedback.management.commands.seed_demo_beverage import simulated_responses

        number, values, consent = simulated_responses(random.Random(7), 100)[0]
        self.assertEqual(number, 1)
        self.assertEqual((values["門市"], values["最常購買的品項（可複選）"], values["整體滿意度"], values["等候分鐘數"]),
                         ("台北車站店", ["紅茶"], "6", "10.9"))
        self.assertTrue(consent)

    @cloud_only
    def test_node_create_refused_in_cloud_mode(self):
        with self.assertRaisesMessage(CommandError, "本機節點模式"):
            call_command("seed_demo_beverage", "--node-create", stdout=StringIO())
