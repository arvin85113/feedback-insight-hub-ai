from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from feedback.models import Survey


class Command(BaseCommand):
    help = "開啟單一問卷的收件匣（原型限定，需 CLOUD_SYNC_PROTOTYPE_ENABLED 與 CLOUD_INBOX_ENABLED）。"

    def add_arguments(self, parser):
        parser.add_argument("--survey", required=True, help="問卷 slug")

    def handle(self, *args, **options):
        if not (settings.CLOUD_SYNC_PROTOTYPE_ENABLED and settings.CLOUD_INBOX_ENABLED):
            raise CommandError("收件匣原型未啟用（需 CLOUD_SYNC_PROTOTYPE_ENABLED 與 CLOUD_INBOX_ENABLED）；正式網站本階段不得開啟。")
        survey = Survey.objects.filter(slug=options["survey"]).first()
        if survey is None or survey.owner_node_id is None:
            raise CommandError("找不到問卷，或問卷尚未指派給節點")
        if survey.inbox_since is None:
            survey.inbox_since = timezone.now()
            survey.save(update_fields=["inbox_since"])
        self.stdout.write(f"{survey.slug} 的收件匣自 {survey.inbox_since.isoformat()} 起啟用")
