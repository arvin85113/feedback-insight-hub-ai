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
