from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "已停用：收件匣改為在發布指派節點的問卷時設定（問卷建立工具規格 §4.3、§7.1）。"

    def add_arguments(self, parser):
        parser.add_argument("--survey", required=False, help="問卷 slug")

    def handle(self, *args, **options):
        raise CommandError("已改為發布時設定收件匣，請改用發布")
