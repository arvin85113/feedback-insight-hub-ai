from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from feedback.test_utils import cloud_only


@cloud_only
class SharedLoginEntryTests(TestCase):
    """Managers and customers share one login page; routing follows the account's role."""

    def setUp(self):
        User = get_user_model()
        self.manager = User.objects.create_user(username="entry-manager", password="pass-12345", role="manager")
        self.customer = User.objects.create_user(
            username="entry-customer", password="pass-12345", role="customer", is_email_verified=True
        )
        self.login_url = reverse("accounts:login")

    def login(self, username, next_url=None):
        data = {"username": username, "password": "pass-12345"}
        if next_url:
            data["next"] = next_url
        return self.client.post(self.login_url, data)

    def test_customer_following_the_manager_entry_lands_on_customer_home(self):
        response = self.login("entry-customer", reverse("feedback:dashboard"))
        self.assertRedirects(response, reverse("feedback:customer-home"))

    def test_manager_following_the_customer_entry_lands_on_dashboard(self):
        response = self.login("entry-manager", reverse("feedback:customer-home"))
        self.assertRedirects(response, reverse("feedback:dashboard"))

    def test_matching_entry_and_other_pages_keep_their_next_target(self):
        stats = reverse("feedback:stats-overview")
        self.assertRedirects(self.login("entry-manager", stats), stats)
        self.client.logout()
        notifications = reverse("feedback:customer-notifications")
        self.assertRedirects(self.login("entry-customer", notifications), notifications)

    def test_no_next_routes_by_role(self):
        self.assertRedirects(self.login("entry-manager"), reverse("feedback:dashboard"))
        self.client.logout()
        self.assertRedirects(self.login("entry-customer"), reverse("feedback:customer-home"))

    def test_login_page_is_role_neutral_and_marks_the_chosen_entry(self):
        neutral = self.client.get(self.login_url)
        self.assertContains(neutral, "管理者／顧客共用")
        self.assertNotContains(neutral, "is-intended")
        self.assertNotContains(neutral, ">Workspace<")

        manager_entry = self.client.get(self.login_url, {"next": reverse("feedback:dashboard")})
        self.assertEqual(manager_entry.context["entry_role"], "manager")
        self.assertContains(manager_entry, "你正要前往", count=1)

        customer_entry = self.client.get(self.login_url, {"next": reverse("feedback:customer-home")})
        self.assertEqual(customer_entry.context["entry_role"], "customer")
