"""Aggregate evaluation records into the table published in docs/ai-eval.md (numbers only, no text)."""

from collections import defaultdict
from statistics import mean

from .strategies import STRATEGIES

RESULTS_START = "<!-- ai-eval-results:start -->"
RESULTS_END = "<!-- ai-eval-results:end -->"


def _avg(values):
    values = [value for value in values if value is not None]
    return round(mean(values), 2) if values else None


def summarize(records):
    groups = defaultdict(list)
    for record in records:
        groups[(record["survey"], record["strategy"])].append(record)
    rows = []
    for (survey, strategy), items in sorted(groups.items()):
        published = [item for item in items if item["outcome"] == "published"]
        rows.append({
            "survey": survey,
            "strategy": strategy,
            "runs": len(items),
            "published_rate": round(len(published) / len(items), 3),
            "kept_items": _avg([item.get("kept_findings", 0) + item.get("kept_drafts", 0) for item in published]),
            "discarded_items": _avg([item.get("discarded_items") for item in items]),
            "ungrounded_numbers": sum(
                len(item.get("discarded_numbers") or []) + len(item.get("ungrounded_numbers") or []) for item in items
            ),
            "unresolved_placeholders": sum(item.get("unresolved_placeholders", 0) for item in items),
            "raw_digit_tokens": sum(item.get("raw_digit_tokens", 0) for item in items),
            "numeric_share": _avg([item.get("numeric_share") for item in published]),
            "latency_s": _avg([(item.get("latency_ms") or 0) / 1000 for item in items if "latency_ms" in item]),
            "prompt_tokens": _avg([item.get("prompt_tokens") for item in items]),
            "output_tokens": _avg([item.get("output_tokens") for item in items]),
            "failures": sorted({item.get("reason") for item in items if item["outcome"] != "published"} - {None}),
        })
    return rows


def _pct(value):
    return "—" if value is None else f"{value * 100:.0f}%"


def _num(value):
    return "—" if value is None else f"{value:g}"


def render_results(records, *, model, generated_at=""):
    lines = [
        f"模型：`{model}`　每組重複 {max((r['repeat'] for r in records), default=0)} 次　{generated_at}".rstrip(),
        "",
        "| 問卷 | 寫法 | 次數 | 發布率 | 保留項目 | 捨棄項目 | 未對應證據的數字 | 無效代號 | 直接寫出的數字 | 含具體數字的項目 | 延遲（秒） | 輸入／輸出 token | 失敗原因 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in summarize(records):
        label = STRATEGIES[row["strategy"]].label if row["strategy"] in STRATEGIES else row["strategy"]
        lines.append(
            f"| {row['survey']} | {label} | {row['runs']} | {_pct(row['published_rate'])} | {_num(row['kept_items'])} | "
            f"{_num(row['discarded_items'])} | {row['ungrounded_numbers']} | {row['unresolved_placeholders']} | "
            f"{row['raw_digit_tokens']} | {_pct(row['numeric_share'])} | {_num(row['latency_s'])} | "
            f"{_num(row['prompt_tokens'])}／{_num(row['output_tokens'])} | {'、'.join(row['failures']) or '—'} |"
        )
    return "\n".join(lines) + "\n"


def write_report(path, records, *, model, generated_at):
    block = f"{RESULTS_START}\n{render_results(records, model=model, generated_at=generated_at)}{RESULTS_END}"
    text = path.read_text(encoding="utf-8") if path.exists() else f"# AI 輸出評估\n\n{RESULTS_START}\n{RESULTS_END}\n"
    if RESULTS_START in text and RESULTS_END in text:
        before, rest = text.split(RESULTS_START, 1)
        after = rest.split(RESULTS_END, 1)[1]
        text = before + block + after
    else:
        text = text.rstrip("\n") + "\n\n" + block + "\n"
    path.write_bytes(text.encode("utf-8"))
