from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import FeedbackSubmission, Survey
from .test_utils import published


class SurveyManagerListTests(TestCase):
    def setUp(self):
        manager = get_user_model().objects.create_user(username="list-manager", password="pass", role="manager")
        self.client.force_login(manager)
        self.live = published(Survey.objects.create(title="門市體驗", slug="live", description="外帶與內用"))
        self.draft = Survey.objects.create(title="新品試喝", slug="draft")
        self.off = published(Survey.objects.create(title="舊活動", slug="off"))
        Survey.objects.filter(pk=self.off.pk).update(is_active=False)
        for days in (0, 0, 2, 9):
            submission = FeedbackSubmission.objects.create(survey=self.live)
            FeedbackSubmission.objects.filter(pk=submission.pk).update(
                submitted_at=timezone.now() - timedelta(days=days))

    def get(self, **params):
        return self.client.get(reverse("feedback:survey-manager"), params)

    def slugs(self, response):
        return [survey.slug for survey in response.context["surveys"]]

    def test_rows_carry_status_counts_and_seven_day_trend(self):
        response = self.get()
        rows = {survey.slug: survey for survey in response.context["surveys"]}
        self.assertEqual(
            {slug: row.status for slug, row in rows.items()}, {"live": "live", "draft": "draft", "off": "inactive"}
        )
        self.assertEqual(rows["live"].submission_count, 4)
        self.assertEqual(rows["live"].recent_total, 3)
        self.assertEqual(len(rows["live"].trend_bars), 7)
        self.assertEqual(rows["live"].trend_bars[-1], {"total": 2, "percent": 100})
        tabs = {tab["key"]: tab["count"] for tab in response.context["status_tabs"]}
        self.assertEqual(tabs, {"": 3, "live": 1, "draft": 1, "inactive": 1})

    def test_status_tab_search_and_sort(self):
        self.assertEqual(self.slugs(self.get(status="draft")), ["draft"])
        self.assertEqual(self.slugs(self.get(q="外帶")), ["live"])
        self.assertEqual(self.slugs(self.get(sort="responses"))[0], "live")
        # Unknown values fall back to all surveys, most recent responses first.
        response = self.get(status="nope", sort="nope")
        self.assertEqual(response.context["current_status"], "")
        self.assertEqual(self.slugs(response)[0], "live")

    def test_draft_is_deleted_and_published_survey_is_archived(self):
        response = self.get()
        self.assertContains(response, ">刪除草稿</button>", count=1)
        self.assertContains(response, ">封存問卷</button>", count=2)

    def test_empty_filter_offers_to_clear_it(self):
        response = self.get(q="不存在的問卷")
        self.assertContains(response, "沒有符合條件的問卷")
        self.assertContains(response, "清除篩選")
