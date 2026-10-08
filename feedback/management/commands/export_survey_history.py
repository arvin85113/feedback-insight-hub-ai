import json

from django.core.management.base import BaseCommand, CommandError

from feedback.history_transfer import HistoryError, export_history


class Command(BaseCommand):
    help = "唯讀匯出指定雲端問卷與舊回答；不改指派、不搬移、不刪資料。資料包仍可能含個資。"

    def add_arguments(self, parser):
        parser.add_argument("--survey", action="append", required=True, help="指定 slug；多份可重複提供")
        parser.add_argument("--origin", required=True, help="雲端 HTTPS 網站根網址，不含憑證")
        parser.add_argument("--output", required=True, help="新的 ZIP 檔，不覆蓋既有檔案")
        parser.add_argument("--include-contacts", action="store_true", help="另經授權後包含姓名與 Email；預設不包含")

    def handle(self, *args, **options):
        try:
            result = export_history(options["survey"], options["output"], origin=options["origin"],
                                    contacts=options["include_contacts"])
        except (HistoryError, OSError) as exc:
            raise CommandError("歷史資料匯出失敗；請核對目標與安全上限。" if isinstance(exc, OSError) else str(exc)) from exc
        self.stdout.write(json.dumps(result, ensure_ascii=False))
