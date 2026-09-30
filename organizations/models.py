from django.conf import settings
from django.db import models
from django.db.models import Q


class Organization(models.Model):
    name = models.CharField("組織名稱", max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "組織"
        verbose_name_plural = "組織"

    def __str__(self):
        return self.name

    @classmethod
    def current(cls):
        """The single organization a node serves (None before first-run setup)."""

        return cls.objects.order_by("pk").first()


class OrganizationMembership(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "擁有者"
        ADMIN = "admin", "組織管理員"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="organization_memberships"
    )
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField("角色", max_length=20, choices=Role.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "組織成員"
        verbose_name_plural = "組織成員"
        constraints = [
            models.UniqueConstraint(fields=["user", "organization"], name="organization_membership_unique_user"),
            models.UniqueConstraint(
                fields=["organization"],
                condition=Q(role="owner"),
                name="organization_single_owner",
            ),
        ]

    def __str__(self):
        return f"{self.user} · {self.get_role_display()}"
