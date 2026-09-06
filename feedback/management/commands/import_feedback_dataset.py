from django.core.management.base import BaseCommand, CommandError

from feedback.importing.mapping import MappingConfigError, load_mapping
from feedback.importing.readers import DatasetReaderError, inspect_schema
from feedback.importing.service import import_dataset, preview_dataset


class Command(BaseCommand):
    help = "依 JSON mapping 將 CSV／JSONL／JSONL.GZ／Parquet 外部資料匯入既有問卷模型。"

    def add_arguments(self, parser):
        parser.add_argument("--input", required=True, help="CSV、JSONL、JSONL.GZ 或 Parquet 輸入檔")
        parser.add_argument("--mapping", required=True, help="JSON 欄位映射設定")
        parser.add_argument("--dry-run", action="store_true", help="完整解析與驗證，但不寫入資料庫")
        parser.add_argument("--schema-only", action="store_true", help="只顯示安全的欄位結構摘要")
        parser.add_argument("--limit", type=int, default=1000, help="等機率抽樣筆數上限（預設 1000）")
        parser.add_argument("--all-rows", action="store_true", help="匯入全部有效唯一資料，不進行抽樣")
        parser.add_argument("--seed", type=int, default=42, help="隨機種子（預設 42）")
        parser.add_argument("--batch-size", type=int, default=100, help="每個資料庫 transaction 的筆數")

    def handle(self, *args, **options):
        try:
            mapping = load_mapping(options["mapping"])
            if not options["all_rows"] and options["limit"] < 1:
                raise ValueError("--limit 必須 >= 1")
            if not 1 <= options["batch_size"] <= 5000:
                raise ValueError("--batch-size 必須介於 1 到 5000")

            if options["schema_only"]:
                schema = inspect_schema(options["input"])
                self.stdout.write(f"列數：{schema['row_count']}")
                self.stdout.write(f"無效列數：{schema['invalid_rows']}")
                self.stdout.write("欄位結構：")
                for field in schema["fields"]:
                    types = ",".join(field["types"])
                    self.stdout.write(
                        f"  {field['name']}: types={types}, null_count={field['null_count']}"
                    )
                return

            if options["dry_run"]:
                result = preview_dataset(
                    options["input"],
                    mapping,
                    limit=None if options["all_rows"] else options["limit"],
                    seed=options["seed"],
                )
                self.stdout.write(self.style.WARNING("DRY-RUN：資料庫零寫入"))
            else:
                result = import_dataset(
                    options["input"],
                    mapping,
                    limit=None if options["all_rows"] else options["limit"],
                    seed=options["seed"],
                    batch_size=options["batch_size"],
                )
                self.stdout.write(self.style.SUCCESS("匯入完成"))

            self._write_summary(result)
        except (DatasetReaderError, MappingConfigError, OSError, UnicodeError, ValueError) as exc:
            raise CommandError(str(exc)) from exc

    def _write_summary(self, result):
        self.stdout.write(f"讀取筆數：{result.read_count}")
        self.stdout.write(f"有效唯一筆數：{result.valid_count}")
        self.stdout.write(f"抽樣筆數：{result.sampled_count}")
        self.stdout.write(f"新增筆數：{result.imported_count}")
        self.stdout.write(f"跳過筆數：{result.skipped_count}")
        self.stdout.write(f"重複筆數：{result.duplicate_count}")
        for reason, count in sorted(result.skip_reasons.items()):
            self.stdout.write(f"  跳過原因 {reason}：{count}")
        for field_name, count in sorted(result.missing_answer_counts.items()):
            self.stdout.write(f"  可選欄位缺失 {field_name}：{count}")
        if result.batch is not None:
            self.stdout.write(f"匯入批次 ID：{result.batch.pk}")
            self.stdout.write(f"問卷：{result.survey.title} ({result.survey.slug})")
