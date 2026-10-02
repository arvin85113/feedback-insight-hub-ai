"""Evaluate synthesis output strategies on frozen production inputs."""

import json
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from feedback.ai_eval.cases import build_case, snapshot_for
from feedback.ai_eval.providers import GeminiProvider, ReplayProvider
from feedback.ai_eval.report import write_report
from feedback.ai_eval.runner import run_one
from feedback.ai_eval.strategies import STRATEGIES
from feedback.evidence_projection import estimate_input_tokens
from feedback.models import Survey


class Command(BaseCommand):
    help = "以相同輸入與正式驗證規則比較 AI 綜合解析的寫法；文字結果只存本機，報告只放彙總數字。"

    def add_arguments(self, parser):
        parser.add_argument("--surveys", required=True, help="問卷 slug，以逗號分隔")
        parser.add_argument("--strategies", default="grounded,placeholder")
        parser.add_argument("--repeats", type=int, default=3)
        parser.add_argument("--provider", choices=("gemini", "ollama"), default="gemini")
        parser.add_argument("--model", default="", help="預設為 GEMINI_MODEL")
        parser.add_argument("--allow-paid-ai", action="store_true", help="確認會使用付費 Gemini 額度")
        parser.add_argument("--dry-run", action="store_true", help="只建立測試案例，不呼叫模型")
        parser.add_argument("--replay", default="", help="用先前結果檔保存的原始輸出重新評分，不呼叫模型")
        parser.add_argument("--output-dir", default=str(Path(settings.BASE_DIR) / "data" / "local" / "ai-eval"))
        parser.add_argument("--report", default=str(Path(settings.BASE_DIR) / "docs" / "ai-eval.md"))

    def handle(self, *args, **options):
        strategies = [STRATEGIES.get(key.strip()) for key in options["strategies"].split(",")]
        if None in strategies:
            raise CommandError(f"未知的寫法；可用：{', '.join(STRATEGIES)}")
        if options["repeats"] < 1:
            raise CommandError("--repeats 至少為 1")
        output_dir = Path(options["output_dir"])
        if options["replay"]:
            return self._replay(Path(options["replay"]), output_dir, Path(options["report"]))
        cases = []
        for slug in [value.strip() for value in options["surveys"].split(",") if value.strip()]:
            survey = Survey.objects.filter(slug=slug).first()
            snapshot = survey and snapshot_for(survey)
            if snapshot is None:
                raise CommandError(f"{slug}：找不到已完成統計與文字階段的 AI 快照")
            case = build_case(snapshot)
            path = output_dir / "cases" / f"{slug}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
            cases.append(case)
            tokens = estimate_input_tokens(json.dumps(case["provider_input"], ensure_ascii=False))
            self.stdout.write(f"{slug}：evidence {len(case['aliases'])} 筆，輸入約 {tokens} token")

        calls = len(cases) * len(strategies) * options["repeats"]
        if options["dry_run"]:
            self.stdout.write(f"dry-run：共需 {calls} 次模型呼叫，未呼叫任何模型。")
            return
        if options["provider"] == "ollama":
            raise CommandError("本機模型尚未啟用：Ollama 已安裝但未啟動、沒有下載模型（見 docs/ai-eval.md）。")
        if not options["allow_paid_ai"]:
            raise CommandError(f"將呼叫付費 Gemini {calls} 次；確認後加上 --allow-paid-ai。")
        provider = GeminiProvider(options["model"] or None)

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        results_path = output_dir / f"{stamp}-results.jsonl"
        records = []
        with results_path.open("w", encoding="utf-8") as handle:
            for case in cases:
                for strategy in strategies:
                    for repeat in range(1, options["repeats"] + 1):
                        record = run_one(provider, case, strategy, repeat=repeat)
                        records.append(record)
                        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                        handle.flush()
                        self.stdout.write(
                            f"{case['survey_slug']}｜{strategy.key}｜第 {repeat} 次：{record['outcome']}"
                            + (f"（{record.get('reason')}）" if record["outcome"] != "published" else "")
                        )
        write_report(Path(options["report"]), records, model=provider.model, generated_at=stamp)
        self.stdout.write(f"結果：{results_path}；報告已更新：{options['report']}")

    def _replay(self, source, output_dir, report_path):
        """Re-score saved raw outputs with the current strategies and validators against the saved cases."""

        saved = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
        cases = {}
        records, skipped = [], 0
        for old in saved:
            if "raw_payload" not in old:
                skipped += 1
                continue
            slug = old["survey"]
            if slug not in cases:
                case_path = output_dir / "cases" / f"{slug}.json"
                if not case_path.is_file():
                    raise CommandError(f"{slug}：找不到保存的測試案例 {case_path}")
                cases[slug] = json.loads(case_path.read_text(encoding="utf-8"))
            record = run_one(ReplayProvider(old), cases[slug], STRATEGIES[old["strategy"]], repeat=old["repeat"])
            record["model"] = old.get("model", record["model"])
            records.append(record)
            self.stdout.write(f"{slug}｜{old['strategy']}｜第 {old['repeat']} 次：{record['outcome']}")
        if not records:
            raise CommandError("結果檔沒有保存原始輸出，無法重播")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        results_path = output_dir / f"{stamp}-replay-results.jsonl"
        results_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
        write_report(report_path, records, model=records[0]["model"], generated_at=f"{stamp}（重播 {source.name}）")
        self.stdout.write(f"重播 {len(records)} 筆（略過 {skipped} 筆無原始輸出）；報告已更新：{report_path}")
