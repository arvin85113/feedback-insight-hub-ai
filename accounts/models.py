from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        CUSTOMER = "customer", "顧客"
        MANAGER = "manager", "管理者"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.CUSTOMER)
    organization = models.CharField(max_length=255, blank=True)
    notification_opt_in = models.BooleanField(default=True)
    is_email_verified = models.BooleanField(default=False)

    @property
    def is_manager(self):
        return self.role == self.Role.MANAGER or self.is_staff
