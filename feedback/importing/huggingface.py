import csv
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import requests

from .mapping import ImportMapping


DATASET_VIEWER_BASE = "https://datasets-server.huggingface.co"
HUGGING_FACE_API_BASE = "https://huggingface.co/api/datasets"
FIELD_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class HuggingFacePreparationError(ValueError):
    pass


@dataclass(frozen=True)
class PreparedSampleResult:
    dataset: str
    revision: str
    config: str
    split: str
    requested_count: int
    output_count: int
    random_seed: int
    output_sha256: str


def _get_json(url, *, params=None):
    try:
        response = requests.get(
            url,
            params=params,
            headers={"User-Agent": "FeedbackInsightHub-dataset-preparation/1.0"},
            timeout=(5, 60),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise HuggingFacePreparationError("無法讀取 Hugging Face 資料集資訊") from exc
    if not isinstance(payload, dict):
        raise HuggingFacePreparationError("Hugging Face 回應格式不正確")
    return payload


def resolve_parquet_urls(mapping: ImportMapping):
    preparation = mapping.huggingface_preparation
    if preparation is None:
        raise HuggingFacePreparationError("mapping 未設定 huggingface_preparation")

    metadata = _get_json(f"{HUGGING_FACE_API_BASE}/{mapping.dataset.name}")
    current_revision = metadata.get("sha")
    if current_revision != mapping.dataset.version:
        raise HuggingFacePreparationError(
            "資料集最新 revision 已變更；請先重新執行唯讀驗證，不得沿用舊 mapping"
        )

    parquet_payload = _get_json(
        f"{DATASET_VIEWER_BASE}/parquet",
        params={"dataset": mapping.dataset.name},
    )
    urls = [
        item.get("url")
        for item in parquet_payload.get("parquet_files", [])
        if item.get("config") == preparation.config
        and item.get("split") == preparation.split
        and isinstance(item.get("url"), str)
        and item["url"].startswith("https://huggingface.co/")
    ]
    if not urls:
        raise HuggingFacePreparationError("找不到指定 config／split 的 Dataset Viewer Parquet")
    return tuple(urls)


def _identifier(value):
    if not FIELD_PATTERN.fullmatch(value):
        raise HuggingFacePreparationError("mapping 含不安全的欄位名稱")
    return f'"{value}"'


def _literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_parquet_sample(mapping, parquet_urls, output_path, *, limit, seed):
    preparation = mapping.huggingface_preparation
    if preparation is None:
        raise HuggingFacePreparationError("mapping 未設定 huggingface_preparation")
    if not 1 <= limit <= 50000:
        raise HuggingFacePreparationError("limit 必須介於 1 到 50000")
    if not -(2**31) <= seed < 2**31:
        raise HuggingFacePreparationError("seed 必須是 32-bit 整數")

    destination = Path(output_path).resolve()
    if destination.suffix.casefold() != ".csv":
        raise HuggingFacePreparationError("輸出檔必須是 CSV")
    if destination.exists():
        raise HuggingFacePreparationError("輸出檔已存在；為避免覆寫請改用新路徑")
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        import duckdb
    except ImportError as exc:
        raise HuggingFacePreparationError("缺少 duckdb，請先安裝 requirements.txt") from exc

    urls = tuple(str(item) for item in parquet_urls)
    if not urls:
        raise HuggingFacePreparationError("沒有可讀取的 Parquet")
    for item in urls:
        if not (item.startswith("https://huggingface.co/") or Path(item).is_file()):
            raise HuggingFacePreparationError("Parquet 必須來自 Hugging Face 或本地測試檔")

    quoted_urls = ", ".join(_literal(item) for item in urls)
    selected_fields = ", ".join(_identifier(item) for item in preparation.output_fields)
    required_predicate = " AND ".join(
        f"{_identifier(item)} IS NOT NULL" for item in preparation.required_fields
    ) or "TRUE"
    identity_values = ", ".join(
        f"COALESCE(CAST({_identifier(item)} AS VARCHAR), '<NULL>')"
        for item in preparation.identity_hash_source_fields
    )
    identity_expression = (
        f"sha256(to_json(list_value({identity_values}))) "
        f"AS {_identifier(preparation.identity_hash_output_field)}"
    )
    output_literal = _literal(destination.as_posix())

    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET threads = 1")
        connection.execute("SET memory_limit = '256MB'")
        source_sql = f"read_parquet([{quoted_urls}], union_by_name=true)"
        available_fields = {
            row[0]
            for row in connection.execute(f"DESCRIBE SELECT * FROM {source_sql}").fetchall()
        }
        required_source_fields = set(preparation.output_fields) | set(
            preparation.identity_hash_source_fields
        )
        missing = required_source_fields - available_fields
        if missing:
            names = ", ".join(sorted(missing))
            raise HuggingFacePreparationError(f"Parquet 缺少必要欄位：{names}")

        query = f"""
            COPY (
                WITH eligible AS (
                    SELECT {selected_fields}, {identity_expression}
                    FROM {source_sql}
                    WHERE {required_predicate}
                ), sampled AS (
                    SELECT *
                    FROM eligible
                    USING SAMPLE reservoir({int(limit)} ROWS) REPEATABLE ({int(seed)})
                )
                SELECT * FROM sampled
                ORDER BY {_identifier(preparation.identity_hash_output_field)}
            ) TO {output_literal} (FORMAT CSV, HEADER TRUE)
        """
        connection.execute(query)
    except HuggingFacePreparationError:
        raise
    except Exception as exc:
        if destination.exists():
            destination.unlink()
        raise HuggingFacePreparationError("Parquet 串流抽樣失敗") from exc
    finally:
        connection.close()

    with destination.open("r", encoding="utf-8", newline="") as handle:
        output_count = sum(1 for _row in csv.DictReader(handle))
    return PreparedSampleResult(
        dataset=mapping.dataset.name,
        revision=mapping.dataset.version,
        config=preparation.config,
        split=preparation.split,
        requested_count=limit,
        output_count=output_count,
        random_seed=seed,
        output_sha256=_file_sha256(destination),
    )


def prepare_huggingface_sample(mapping, output_path, *, limit, seed):
    parquet_urls = resolve_parquet_urls(mapping)
    return prepare_parquet_sample(mapping, parquet_urls, output_path, limit=limit, seed=seed)
