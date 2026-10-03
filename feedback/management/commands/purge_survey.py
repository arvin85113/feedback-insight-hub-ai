from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from feedback.models import Survey
from feedback.survey_purge import PurgeRefused, purge_survey


class Command(BaseCommand):
    help = "清除測試或模擬問卷及其所有關聯資料；預設只列出筆數，加 --confirm 才刪除。"

    def add_arguments(self, parser):
        parser.add_argument("--survey", required=True, help="問卷 slug")
        parser.add_argument("--confirm", action="store_true", help="實際刪除（未加時為 dry-run）")

    def handle(self, *args, **options):
        settings_dict = connection.settings_dict
        self.stdout.write(
            f"目前使用資料庫：alias={connection.alias!r}, "
            f"ENGINE={settings_dict.get('ENGINE', '')!r}, NAME={settings_dict.get('NAME', '')!r}"
        )
        survey = Survey.objects.filter(slug=options["survey"]).first()
        if survey is None:
            raise CommandError(f"找不到問卷：{options['survey']}")
        dry_run = not options["confirm"]
        try:
            counts = purge_survey(survey, dry_run=dry_run)
        except PurgeRefused as exc:
            raise CommandError(str(exc)) from exc
        for label, total in sorted(counts.items()):
            self.stdout.write(f"  - {label}: {total}")
        if dry_run:
            self.stdout.write(self.style.WARNING("dry-run：未刪除任何資料，加 --confirm 才會執行"))
        else:
            self.stdout.write(self.style.SUCCESS(f"已清除問卷「{survey.title}」。"))
