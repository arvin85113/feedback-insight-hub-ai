import os

from django.core.management.base import BaseCommand

from accounts.models import User


class Command(BaseCommand):
    help = "從環境變數建立或更新管理員帳號"

    def handle(self, *args, **options):
        username = os.getenv("ADMIN_USERNAME")
        email = os.getenv("ADMIN_EMAIL")
        password = os.getenv("ADMIN_PASSWORD")

        if not username or not email or not password:
            self.stdout.write("略過 superuser 建立：未提供 ADMIN_USERNAME / ADMIN_EMAIL / ADMIN_PASSWORD")
            return

        user, created = User.objects.get_or_create(
            username=username,
            defaults={
                "email": email,
                "role": User.Role.MANAGER,
                "is_staff": True,
                "is_superuser": True,
                "is_email_verified": True,
            },
        )

        user.email = email
        user.role = User.Role.MANAGER
        user.is_staff = True
        user.is_superuser = True
        user.is_email_verified = True
        user.set_password(password)
        user.save()

        action = "建立" if created else "更新"
        self.stdout.write(self.style.SUCCESS(f"已{action}管理員帳號：{username}"))
