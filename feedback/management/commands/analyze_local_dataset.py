import json
import time
from django.core.management.base import BaseCommand
from django.db import connections
from contextlib import ExitStack

from feedback.analysis_adapters import ParquetInput
from feedback.background_analysis import run_once


class Command(BaseCommand):
    help = "單次本機統計、英文詞典 NLP、既有 AI schema 組裝與 mock 驗證。"
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--manifest", required=True)
        parser.add_argument("--mapping", required=True)
        parser.add_argument("--output", required=True)
        parser.add_argument("--mock-ai", action="store_true", required=True)

    def handle(self, *args, **options):
        def deny_db(*args, **kwargs):
            raise RuntimeError("本機分析 CLI 禁止資料庫存取")
        started = time.perf_counter()
        with ExitStack() as stack:
            for connection in connections.all():
                stack.enter_context(connection.execute_wrapper(deny_db))
            adapter = ParquetInput(options["manifest"], options["mapping"])
            acquired = time.perf_counter()
            path, hit = run_once(adapter, options["output"])
        result = json.loads(path.read_text(encoding="utf-8"))["result"]
        self.stdout.write(json.dumps(dict(path=str(path), cache_hit=hit, rows=result["input_rows"],
            acquisition_seconds=round(acquired-started, 3),
            timings_seconds={} if hit else result["timings_seconds"],
            text_coverage=result["text"]["payload"]["coverage"], ai_mode="mock"), ensure_ascii=False))
