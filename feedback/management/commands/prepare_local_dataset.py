from django.core.management.base import BaseCommand, CommandError

from feedback.importing.local_dataset import LocalDatasetError, prepare_local_dataset


class Command(BaseCommand):
    help = "下載固定版本 Parquet，產生本機全量驗證報告與去識別清理 Parquet。"

    def add_arguments(self, parser):
        parser.add_argument("--source-lock", required=True, help="不可變來源與分片鎖定檔")
        parser.add_argument("--output-root", required=True, help="raw/clean/report/manifest 的本機根目錄")

    def handle(self, *args, **options):
        try:
            result = prepare_local_dataset(options["source_lock"], options["output_root"])
        except LocalDatasetError as exc:
            raise CommandError(str(exc)) from exc

        state = "快取重用" if result.cache_hit else "建立完成"
        self.stdout.write(self.style.SUCCESS(f"本機 Parquet 資料層：{state}"))
        self.stdout.write(f"輸入列數：{result.input_rows}")
        self.stdout.write(f"保留列數：{result.retained_rows}")
        self.stdout.write(f"clean：{result.clean_path}")
        self.stdout.write(f"report：{result.report_path}")
        self.stdout.write(f"manifest：{result.manifest_path}")
        self.stdout.write(f"本次耗時秒數：{result.duration_seconds:.3f}")
