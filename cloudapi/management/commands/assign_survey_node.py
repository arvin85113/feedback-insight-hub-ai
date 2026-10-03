from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from cloudapi.errors import PublishedLocked
from cloudapi.models import NodeDevice
from cloudapi.writes import assign_survey_to_node
from feedback.models import Survey


class Command(BaseCommand):
    help = "把問卷指派給本機節點（原型限定，需 CLOUD_SYNC_PROTOTYPE_ENABLED）。"

    def add_arguments(self, parser):
        parser.add_argument("--survey", required=True, help="問卷 slug")
        parser.add_argument("--node", required=True, help="節點名稱")

    def handle(self, *args, **options):
        if not settings.CLOUD_SYNC_PROTOTYPE_ENABLED:
            raise CommandError("雲端同步原型未啟用（CLOUD_SYNC_PROTOTYPE_ENABLED）；正式網站本階段不得指派問卷。")
        survey = Survey.objects.filter(slug=options["survey"]).first()
        node = NodeDevice.objects.filter(name=options["node"]).first()
        if survey is None or node is None:
            raise CommandError("找不到問卷或節點")
        try:
            revision = assign_survey_to_node(survey, node)
        except PublishedLocked as exc:
            raise CommandError("問卷已發布，收件節點不能變更；請複製為新草稿再指派") from exc
        self.stdout.write(f"{survey.slug} 已指派給 {node.name}，版本 {revision.version}")
