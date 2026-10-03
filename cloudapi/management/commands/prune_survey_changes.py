from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from cloudapi.models import ChangeClock, SurveyChange


class Command(BaseCommand):
    help = "刪除超過保留期的問卷變更紀錄；更早的游標之後回 410。"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=90)

    @transaction.atomic
    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=options["days"])
        old = SurveyChange.objects.filter(created_at__lt=cutoff)
        last = old.order_by("-seq").values_list("seq", flat=True).first()
        if last is None:
            self.stdout.write("沒有需要刪除的紀錄")
            return
        count = old.count()
        old.delete()
        clock = ChangeClock.objects.select_for_update().get(pk=1)
        clock.pruned_through = max(clock.pruned_through, last)
        clock.save(update_fields=["pruned_through"])
        self.stdout.write(f"已刪除 {count} 筆，保留期起點序號 {last}")
