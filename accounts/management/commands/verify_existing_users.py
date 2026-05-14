from django.core.management.base import BaseCommand

from accounts.models import User


class Command(BaseCommand):
    help = "將所有現有用戶的 is_email_verified 設為 True，避免升級後被鎖定"

    def handle(self, *args, **options):
        updated = User.objects.filter(is_email_verified=False).update(is_email_verified=True)
        self.stdout.write(self.style.SUCCESS(f"已驗證 {updated} 位用戶。"))
