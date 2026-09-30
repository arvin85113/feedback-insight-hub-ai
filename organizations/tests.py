from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from organizations.access import organization_role
from organizations.models import Organization, OrganizationMembership

Role = OrganizationMembership.Role


class OrganizationMembershipTests(TestCase):
    def setUp(self):
        users = get_user_model().objects
        self.owner = users.create_user(username="owner", password="x")
        self.second = users.create_user(username="second", password="x")
        self.organization = Organization.objects.create(name="Acme")

    def test_only_one_owner_per_organization(self):
        OrganizationMembership.objects.create(user=self.owner, organization=self.organization, role=Role.OWNER)
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrganizationMembership.objects.create(user=self.second, organization=self.organization, role=Role.OWNER)
        OrganizationMembership.objects.create(user=self.second, organization=self.organization, role=Role.ADMIN)

    def test_role_lookup(self):
        OrganizationMembership.objects.create(user=self.owner, organization=self.organization, role=Role.OWNER)
        self.assertEqual(organization_role(self.owner), "owner")
        self.assertIsNone(organization_role(self.second))
        self.assertEqual(Organization.current(), self.organization)
