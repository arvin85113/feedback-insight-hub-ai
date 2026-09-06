from django.core.management.base import BaseCommand, CommandError

from feedback.importing.huggingface import (
    HuggingFacePreparationError,
    prepare_huggingface_sample,
)
from feedback.importing.mapping import MappingConfigError, load_mapping


class Command(BaseCommand):
    help = "從 Hugging Face Dataset Viewer Parquet 串流建立可重現且去識別的 CSV 樣本。"

    def add_arguments(self, parser):
        parser.add_argument("--mapping", required=True, help="含 huggingface_preparation 的 mapping")
        parser.add_argument("--output", required=True, help="新的 CSV 輸出路徑；不覆寫既有檔案")
        parser.add_argument("--limit", type=int, default=5000, help="等機率抽樣筆數（預設 5000）")
        parser.add_argument("--seed", type=int, default=42, help="隨機種子（預設 42）")

    def handle(self, *args, **options):
        try:
            mapping = load_mapping(options["mapping"])
            result = prepare_huggingface_sample(
                mapping,
                options["output"],
                limit=options["limit"],
                seed=options["seed"],
            )
        except (HuggingFacePreparationError, MappingConfigError, OSError) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS("遠端 Parquet 樣本準備完成"))
        self.stdout.write(f"資料集：{result.dataset}")
        self.stdout.write(f"revision：{result.revision}")
        self.stdout.write(f"config／split：{result.config}／{result.split}")
        self.stdout.write(f"seed：{result.random_seed}")
        self.stdout.write(f"輸出筆數：{result.output_count}")
        self.stdout.write(f"輸出 SHA-256：{result.output_sha256}")
