"""Single-run local analysis. Mock AI only; no DB persistence or provider calls."""
import copy
import hashlib
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from . import ai_statistics_service, ai_text_service, ai_synthesis_service
from .ai_snapshot_service import (
    SNAPSHOT_SCHEMA_VERSION, STATISTICS_VERSION, _build_text_snapshot,
    build_statistics_snapshot, _clean_text,
)
from .ai_stage_service import _validate_stage_finding
from .local_service import analyze_frame
from .models import Question, recommend_analysis, _resolve_keyword_category
from .text_pipeline import (estimate_sentiment_score, tokenize_feedback, load_positive_words,
    load_negative_words, load_synonyms, _jieba_tokenize)

PIPELINE_VERSION = "local-analysis-mock-v1"
PROFILE_PATH = Path(__file__).parent / "data" / "english_analysis.json"


class LocalAnalysisCancelled(RuntimeError):
    """Raised at a safe checkpoint before a local result is published."""


def _checkpoint(cancel_requested, progress, stage, percent):
    if cancel_requested and cancel_requested():
        raise LocalAnalysisCancelled("local analysis cancelled")
    if progress:
        progress(stage, percent)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def pipeline_version(profile):
    # Include the implementation and all reused calculation/schema/privacy code.
    paths = [Path(__file__), Path(__file__).with_name("analysis_adapters.py"),
             Path(__file__).with_name("analysis_input.py"),
             Path(__file__).with_name("local_service.py"), Path(__file__).with_name("models.py"),
             Path(__file__).with_name("text_pipeline.py"),
             Path(__file__).with_name("ai_snapshot_service.py"),
             Path(__file__).with_name("ai_stage_service.py")]
    paths += [Path(module.__file__) for module in (ai_statistics_service, ai_text_service, ai_synthesis_service)]
    paths += sorted((Path(__file__).parent / "data").glob("*.txt"))
    paths += sorted((Path(__file__).parent / "data").glob("*.json"))
    return digest({"version": PIPELINE_VERSION, "profile": profile,
                   "code": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}})


def descriptors(adapter):
    return [Question(id=index, code=f.name, title=f.title or f.name, kind=f.kind, data_type=f.data_type,
                     options_text="\n".join(f.options), order=index,
                     enable_keyword_tracking=f.tracked)
            for index, f in enumerate(adapter.fields(), 1)]


def calculate_statistics(adapter, questions):
    fields = adapter.fields()
    numeric_fields = [f for f in fields if f.data_type != "text"]
    # Keep row alignment; normalize ordinal encodings consistently across ORM strings and Parquet numbers.
    frame = pd.DataFrame(adapter.scan(tuple(f.name for f in numeric_fields)))
    for index, field in enumerate(fields, 1):
        if field.name not in frame:
            continue
        if field.data_type == "ordinal" and field.kind == "scale":
            def ordinal(value):
                if pd.isna(value):
                    return None
                try:
                    number = float(value)
                    return str(int(number)) if number.is_integer() else str(value)
                except (TypeError, ValueError):
                    return str(value)
            frame[field.name] = frame[field.name].map(ordinal)
    frame.columns = [f"Q_{next(i for i, f in enumerate(fields, 1) if f.name == name)}" for name in frame.columns]
    clean_frame = frame.copy()
    for index, field in enumerate(fields, 1):
        col = f"Q_{index}"
        if col in frame and field.data_type == "ordinal":
            clean_frame[col] = frame[col].where(frame[col].isin(field.options), pd.NA)
    result = analyze_frame(questions, clean_frame)
    result["question_analysis"] = [dict(title=q.title, data_type=q.get_data_type_display(),
                                        analysis=recommend_analysis(q)) for q in questions]
    result["available_tests_count"] = sum(not r.get("skipped_reason") for r in result["inferential_analysis"])
    result["skipped_tests_count"] = len(result["inferential_analysis"]) - result["available_tests_count"]
    coverage = {}
    for index, field in enumerate(fields, 1):
        col = f"Q_{index}"
        if col not in frame:
            continue
        series = frame[col]
        missing = series.isna() | series.astype("string").str.strip().eq("").fillna(False)
        if field.data_type == "ordinal":
            valid = series.isin(field.options)
        elif field.data_type in {"continuous", "discrete"}:
            valid = pd.to_numeric(series, errors="coerce").notna()
        else:
            valid = ~missing
        coverage[field.name] = dict(total_n=len(frame), valid_n=int(valid.sum()),
            missing_n=int(missing.sum()), invalid_n=int((~missing & ~valid).sum()))
    result["field_coverage"] = coverage
    return result, len(frame)


def calculate_text(adapter, profile):
    columns = tuple(f.name for f in adapter.fields() if f.tracked)
    counts, response_counts = Counter(), Counter()
    categories = {word: category for category, words in profile["categories"].items() for word in words}
    # Export only configured vocabulary, so names/addresses and unknown free-text tokens cannot leak.
    allowed = set(categories) | set(profile["positive"]) | set(profile["negative"])
    rules = [SimpleNamespace(**row) for row in getattr(adapter, "keyword_rules", [])]
    chinese_polarity = load_positive_words() | load_negative_words()
    allowed |= chinese_polarity | set(load_synonyms().values()) | {r.keyword for r in rules}
    stop = set(profile["stopwords"])
    bucket = {}
    rule_groups = Counter()
    total = analyzed = known = keyword_docs = raw_tokens = valid_tokens = hits = 0
    sentiment_sum = 0.0
    polarity = set(profile["positive"]) | set(profile["negative"])
    for row in adapter.scan(columns):
        for name in columns:
            value = row.get(name)
            if value is None or not str(value).strip():
                continue
            total += 1
            # Language detection must precede Chinese redaction placeholders.
            chinese = bool(re.search(r"[\u4e00-\u9fff]", str(value)))
            # Redact before tokenization; retain raw text only in input storage.
            text = re.sub(r"https?://\S+", " ", _clean_text(value)).lower()
            tokens = (_jieba_tokenize(text) if chinese else re.findall(r"[a-z]+(?:'[a-z]+)?", text))
            raw_tokens += len(tokens)
            tokens = (tokenize_feedback(text) if chinese else
                      [token for token in tokens if len(token) >= 2 and token not in stop])
            valid_tokens += len(tokens)
            analyzed += bool(tokens)
            selected = [token for token in tokens if token in allowed]
            counts.update(selected)
            response_counts.update(set(selected))
            keyword_docs += bool(selected)
            hits += sum(token in (chinese_polarity if chinese else polarity) for token in tokens)
            score = (estimate_sentiment_score(text) if any(word in text for word in chinese_polarity) else None) if chinese else estimate_sentiment_score(text, profile=profile)
            if score is not None:
                known += 1
                sentiment_sum += score
            label = "unknown" if score is None else "positive" if score > .1 else "negative" if score < -.1 else "neutral"
            if rules:
                rule_groups[(tuple(sorted(set(selected))), label)] += 1
                continue
            for category in {categories.get(token, "未分類") for token in selected} or {"未分類"}:
                item = bucket.setdefault(category, dict(category=category, positive=0, neutral=0,
                    negative=0, unknown=0, total=0))
                item["total"] += 1
                item[label] += 1
    for (tokens, label), count in rule_groups.items():
        for category in {_resolve_keyword_category(token, count=counts[token], rules=rules) for token in tokens} or {"未分類"}:
            item = bucket.setdefault(category, dict(category=category, positive=0, neutral=0,
                negative=0, unknown=0, total=0))
            item["total"] += count
            item[label] += count
    keywords = [dict(keyword=word, count=count, response_count=response_counts[word],
                     category=(_resolve_keyword_category(word, count=count, rules=rules) if rules else categories.get(word, "未分類")))
                for word, count in sorted(counts.items(), key=lambda r: (-r[1], r[0]))[:20]]
    return dict(keywords=keywords,
        summary=dict(total_answers=total, analyzed_answers=analyzed,
            analysis_coverage=round(analyzed / total, 6) if total else 0,
            avg_sentiment_score=round(sentiment_sum / known, 3) if known else None),
        category_sentiments=sorted(bucket.values(), key=lambda r: (-r["total"], r["category"])),
        coverage=dict(raw_tokens=raw_tokens, effective_tokens=valid_tokens,
            effective_token_ratio=valid_tokens / raw_tokens if raw_tokens else 0,
            keyword_documents=keyword_docs, keyword_coverage=keyword_docs / total if total else 0,
            sentiment_documents=known, sentiment_coverage=known / total if total else 0,
            unknown_sentiment_documents=total-known, sentiment_token_hits=hits),
        profile_version=profile["version"], limitations=profile["limitations"])


def assemble_input(adapter, questions, stats, text, row_count, version):
    evidence, caveats = [], [text["limitations"],
        "情緒詞典未命中的評論標示未知，不列入已知情緒比例；覆蓋率不代表準確率。",
        f"資料來源類型：{adapter.source_kind}；來源名稱：{adapter.name}；來源版本：{adapter.dataset_version}"]
    if adapter.source_kind == "external_dataset":
        caveats.append("這是固定歷史外部資料集，不是網站收集的問卷回覆，不代表即時市場。")
    bounded = {**stats, "charts": [{**chart, "counts": chart.get("counts", [])[:20]}
                                   for chart in stats["charts"]]}
    if any(len(c.get("counts", [])) > 20 for c in stats["charts"]):
        caveats.append("AI 數值分布最多提供前二十個值；完整分布保留於統計產物。")
    statistics = build_statistics_snapshot(questions, bounded, evidence, caveats)
    known_text = {**text, "category_sentiments": [
        {**item, "total": item["total"] - item["unknown"]} for item in text["category_sentiments"]]}
    text_snapshot = _build_text_snapshot(known_text, evidence, caveats)
    text_snapshot["coverage"].update(text["coverage"])
    return dict(schema_version=SNAPSHOT_SCHEMA_VERSION, data_fingerprint=version,
        data_scope=dict(survey_slug=None, survey_title=adapter.name, source_kind=adapter.source_kind,
            source_version=adapter.dataset_version, valid_response_count=row_count,
            source_latest_date=None, analysis_coverage=text["summary"]["analysis_coverage"],
            statistics_version=STATISTICS_VERSION, text_analysis_version=text["profile_version"]),
        statistics=statistics, text_analysis=text_snapshot, evidence_catalog=evidence,
        data_caveats=caveats, existing_improvements=[])


def mock_output(module, stage_input):
    """Deterministic schema exercise, never an actual AI conclusion."""
    if module is ai_synthesis_service:
        return dict(executive_summary="模擬輸出：僅驗證分析流程與格式。", combined_findings=[],
                    improvement_drafts=[], data_caveats=["模擬結果不能作為實際營運決策。"])
    result = {key: [] for key in module.SECTIONS}
    if stage_input["evidence_catalog"]:
        result[module.SECTIONS[0]] = [dict(title="模擬輸出", rationale="僅測試已提供的聚合證據引用。",
            evidence_refs=[stage_input["evidence_catalog"][0]["id"]], data_limitations=["這是格式測試。"])]
    return result


def run_mock_stages(snapshot, version, provider=mock_output):
    stages = {}
    for module in (ai_statistics_service, ai_text_service, ai_synthesis_service):
        kind = module.STAGE_TYPE
        if kind == "synthesis":
            upstream = [SimpleNamespace(pk=stages[k]["input_hash"], output_json=stages[k]["output"])
                        for k in ("statistics", "text")]
            stage_input = module.build_input(*upstream, [], snapshot["data_scope"])
            registry = {key: value for k in ("statistics", "text")
                        for key, value in stages[k]["evidence_registry"].items()}
        else:
            stage_input = module.build_input(snapshot)
            # Existing schemas retain compatibility slots; source metadata is always explicit.
            stage_input["data_scope"].update({key: snapshot["data_scope"][key]
                for key in ("source_kind", "source_version")})
            registry = {item["id"]: item for item in stage_input["evidence_catalog"]}
        key = digest(dict(input=stage_input, pipeline=version, schema=module.SCHEMA_VERSION,
                          prompt=module.PROMPT_VERSION, mode="mock"))
        raw = provider(module, stage_input)
        output = (module.validate_output(raw, registry, key) if kind == "synthesis" else
                  module.validate_output(raw, registry, _validate_stage_finding))
        stages[kind] = dict(status="mock_succeeded", mode="mock", input_hash=key,
            schema_version=module.SCHEMA_VERSION, prompt_version=module.PROMPT_VERSION,
            input=stage_input, output=output, evidence_registry=registry,
            response_schema=module.response_schema_for_profile("standard"))
    return stages


def run_once(
    adapter,
    output_root,
    *,
    profile=None,
    provider=mock_output,
    cancel_requested=None,
    progress=None,
):
    _checkpoint(cancel_requested, progress, "準備分析", 3)
    profile = profile or json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    pipeline = pipeline_version(profile)
    version = digest(dict(source=adapter.dataset_version, name=adapter.name, kind=adapter.source_kind,
        fields=[vars(f) for f in adapter.fields()], rules=getattr(adapter, "keyword_rules", []), pipeline=pipeline, mode="mock"))
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{version}.json"
    if destination.exists():
        envelope = json.loads(destination.read_text(encoding="utf-8"))
        if envelope["sha256"] != digest(envelope["result"]) or envelope["result"]["version"] != version:
            raise ValueError("analysis cache integrity mismatch")
        _checkpoint(cancel_requested, progress, "使用既有分析結果", 100)
        return destination, True
    lock = root / f"{version}.lock"
    # No automatic stale-lock stealing; interrupted runs need explicit recovery.
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(descriptor)
    partial = root / f"{version}.part"
    partial_created = False
    try:
        started = time.perf_counter()
        questions = descriptors(adapter)
        _checkpoint(cancel_requested, progress, "計算統計", 12)
        stats, count = calculate_statistics(adapter, questions)
        after_stats = time.perf_counter()
        _checkpoint(cancel_requested, progress, "分析評論文字", 46)
        text = calculate_text(adapter, profile)
        after_text = time.perf_counter()
        _checkpoint(cancel_requested, progress, "整理分析證據", 72)
        snapshot = assemble_input(adapter, questions, stats, text, count, version)
        after_input = time.perf_counter()
        _checkpoint(cancel_requested, progress, "產生測試模式 AI 結果", 84)
        stages = run_mock_stages(snapshot, pipeline, provider)
        after_ai = time.perf_counter()
        _checkpoint(cancel_requested, progress, "確認來源版本", 92)
        if hasattr(adapter, "verify_unchanged"):
            adapter.verify_unchanged()
        serial_stats = {**stats, "charts": [{**chart, "question": dict(title=chart["question"].title,
            kind=chart["question"].kind, data_type=chart["question"].data_type)} for chart in stats["charts"]]}
        result = dict(version=version, pipeline_version=pipeline, ai_mode="mock",
            published_locally_at=datetime.now(timezone.utc).isoformat(), input_rows=count,
            source=dict(kind=adapter.source_kind, name=adapter.name, version=adapter.dataset_version),
            statistics=dict(source_version=adapter.dataset_version, payload=serial_stats),
            text=dict(source_version=adapter.dataset_version, payload=text),
            ai=dict(source_version=adapter.dataset_version, stages=stages),
            timings_seconds=dict(statistics_and_read=after_stats-started, text_and_read=after_text-after_stats,
                input_assembly=after_input-after_text, mock_ai=after_ai-after_input, total=after_ai-started))
        envelope = dict(sha256=digest(result), result=result)
        _checkpoint(cancel_requested, progress, "儲存本機結果", 97)
        with partial.open("x", encoding="utf-8") as handle:
            partial_created = True
            json.dump(envelope, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        _checkpoint(cancel_requested, progress, "發布本機結果", 99)
        os.replace(partial, destination)
        if progress:
            progress("分析完成", 100)
        return destination, False
    finally:
        if partial_created:
            partial.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
