from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import SurveyCategory


class ManagerShellTests(TestCase):
    def setUp(self):
        self.manager = get_user_model().objects.create_user(username="shell-manager", password="pass", role="manager")
        self.client.force_login(self.manager)

    def test_topbar_names_the_active_section(self):
        response = self.client.get(reverse("feedback:stats-overview"))
        self.assertContains(response, '<h1>統計分析</h1>', html=True)
        self.assertContains(response, 'aria-current="page"')

    def test_manager_pages_render_flash_messages(self):
        response = self.client.post(reverse("feedback:category-create"), {"name": "門市"}, follow=True)
        rendered = [str(message) for message in response.context["messages"]]
        self.assertTrue(SurveyCategory.objects.filter(name="門市").exists())
        self.assertTrue(rendered, "the category view should queue a flash message")
        self.assertContains(response, 'class="flash-stack"')
        self.assertContains(response, rendered[0])
        # Rendered once, so it must not linger on the next page.
        self.assertNotContains(self.client.get(reverse("feedback:survey-manager")), rendered[0])


class ExternalSourceResponseCountTests(TestCase):
    """Surveys analysed from a registered external dataset show that dataset's size, not DB rows."""

    def setUp(self):
        from datetime import datetime, timezone

        from .analysis_sources import register_external_dataset_version
        from .models import FeedbackSubmission, Survey

        self.manager = get_user_model().objects.create_user(username="count-manager", password="pass", role="manager")
        self.client.force_login(self.manager)
        self.external = Survey.objects.create(title="External reviews", slug="external-reviews")
        register_external_dataset_version(
            self.external.pk,
            source_ref="fixture/reviews",
            source_version="revision-a:clean-v1:" + "a" * 64,
            source_revision="revision-a",
            cleaning_version="clean-v1",
            content_sha256="a" * 64,
            schema_sha256="b" * 64,
            mapping_key="fixture_mapping",
            mapping_version="fixture-v1",
            row_count=201295,
            source_latest_at=datetime(2012, 12, 20, tzinfo=timezone.utc),
            provenance={"dataset_url": "https://example.test/dataset"},
        )
        self.native = Survey.objects.create(title="Native survey", slug="native-survey")
        FeedbackSubmission.objects.create(survey=self.native)

    def test_survey_manager_lists_dataset_size_for_external_sources(self):
        response = self.client.get(reverse("feedback:survey-manager"))
        rows = {survey.slug: survey for survey in response.context["surveys"]}
        self.assertEqual(rows["external-reviews"].submission_count, 201295)
        self.assertEqual(rows["external-reviews"].latest_submission_at.year, 2012)
        self.assertEqual(rows["native-survey"].submission_count, 1)

    def test_builder_header_uses_dataset_size_for_external_sources(self):
        response = self.client.get(reverse("feedback:survey-builder", args=["external-reviews"]))
        self.assertEqual(response.context["responses_count"], 201295)
        self.assertContains(response, "12/20")
        native = self.client.get(reverse("feedback:survey-builder", args=["native-survey"]))
        self.assertEqual(native.context["responses_count"], 1)
