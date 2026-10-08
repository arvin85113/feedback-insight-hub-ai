import json

from django.core.management.base import BaseCommand, CommandError

from feedback.history_transfer import HistoryBundle, HistoryError, import_history


class Command(BaseCommand):
    help = "驗證歷史資料包；只有 --confirm 才寫入本機歷史副本，不做同步或分析。"

    def add_arguments(self, parser):
        parser.add_argument("--package", required=True, help="歷史資料 ZIP 路徑")
        parser.add_argument("--confirm", action="store_true", help="確認寫入所選本機節點 DB")
        parser.add_argument("--expected-sha256", help="確認匯入時必填，須等於預覽的完整資料包 SHA-256")

    def handle(self, *args, **options):
        try:
            with HistoryBundle(options["package"]) as bundle:
                if options["confirm"]:
                    if options["expected_sha256"] != bundle.sha256:
                        raise HistoryError("請先預覽，再以相同資料包 SHA-256 確認匯入")
                    result = import_history(bundle)
                else:
                    result = bundle.preview()
        except (HistoryError, OSError) as exc:
            raise CommandError("無法讀取資料包。" if isinstance(exc, OSError) else str(exc)) from exc
        self.stdout.write(json.dumps(result, ensure_ascii=False))
