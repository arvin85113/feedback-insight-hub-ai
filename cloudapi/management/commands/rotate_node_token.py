from django.core.management.base import BaseCommand, CommandError

from cloudapi.models import NodeDevice


class Command(BaseCommand):
    help = "為單一節點重新產生權杖並顯示一次（最後一行為權杖本身）；舊權杖立即失效。"

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)

    def handle(self, *args, **options):
        device = NodeDevice.objects.filter(name=options["name"]).first()
        if device is None:
            raise CommandError("找不到節點")
        token = device.rotate()
        self.stdout.write(f"裝置 {device.name} 的新權杖只顯示這一次：")
        self.stdout.write(token)
