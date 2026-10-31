from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import ImprovementUpdate, Survey
from .test_utils import published


class ImprovementListTests(TestCase):
    def setUp(self):
        self.manager = get_user_model().objects.create_user(username="imp-manager", password="pass", role="manager")
        self.client.force_login(self.manager)
        self.survey = published(Survey.objects.create(title="門市體驗", slug="store"))
        for title, status in (("加開收銀", "in_progress"), ("甜度說明", "draft"),
                              ("封膜", "completed"), ("集點卡", "archived")):
            ImprovementUpdate.objects.create(survey=self.survey, title=title, summary="-", status=status,
                                             created_by=self.manager, updated_by=self.manager)

    def test_items_are_grouped_by_status_with_work_in_flight_first(self):
        response = self.client.get(reverse("feedback:improvement-list"), {"survey": "store"})
        groups = [(group["status"], [item.title for item in group["items"]])
                  for group in response.context["improvement_groups"] if group["items"]]
        self.assertEqual(groups, [("in_progress", ["加開收銀"]), ("draft", ["甜度說明"]),
                                  ("completed", ["封膜"]), ("archived", ["集點卡"])])
        self.assertEqual(response.context["open_improvement_total"], 2)
        self.assertContains(response, 'id="improvement-')

    def test_survey_rows_count_open_items(self):
        response = self.client.get(reverse("feedback:improvement-list"))
        row = next(row for row in response.context["improvement_survey_rows"] if row.slug == "store")
        self.assertEqual((row.improvement_count, row.open_improvement_count), (4, 2))
