from django.core.management.base import BaseCommand

from cloudapi.models import NodeDevice


class Command(BaseCommand):
    help = "建立本機節點裝置並顯示一次性權杖（最後一行為權杖本身）。"

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)

    def handle(self, *args, **options):
        device, token = NodeDevice.issue(options["name"])
        self.stdout.write(f"裝置 {device.name}（{device.uuid}）已建立；權杖只顯示這一次：")
        self.stdout.write(token)
