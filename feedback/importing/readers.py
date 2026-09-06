import csv
import gzip
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


class DatasetReaderError(ValueError):
    pass


@dataclass(frozen=True)
class SourceRecord:
    row_number: int
    data: dict | None
    error_code: str = ""


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _format(path):
    lower_name = Path(path).name.lower()
    if lower_name.endswith(".csv"):
        return "csv"
    if lower_name.endswith(".jsonl"):
        return "jsonl"
    if lower_name.endswith(".jsonl.gz"):
        return "jsonl.gz"
    if lower_name.endswith(".parquet"):
        return "parquet"
    raise DatasetReaderError("只支援 CSV、JSONL、JSONL.GZ 與 Parquet")


def iter_records(path):
    source_path = Path(path)
    if not source_path.is_file():
        raise DatasetReaderError(f"找不到輸入檔：{source_path}")
    source_format = _format(source_path)
    if source_format == "parquet":
        try:
            import duckdb

            with duckdb.connect(":memory:") as connection:
                cursor = connection.execute("SELECT * FROM read_parquet(?)", [str(source_path.resolve())])
                field_names = [column[0] for column in cursor.description]
                row_number = 1
                while rows := cursor.fetchmany(2048):
                    for values in rows:
                        yield SourceRecord(row_number=row_number, data=dict(zip(field_names, values)))
                        row_number += 1
        except Exception as exc:
            raise DatasetReaderError("Parquet 無法讀取") from exc
        return
    if source_format == "csv":
        try:
            with source_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames:
                    raise DatasetReaderError("CSV 缺少欄位標題")
                for row_number, row in enumerate(reader, start=2):
                    yield SourceRecord(row_number=row_number, data=dict(row))
        except UnicodeError as exc:
            raise DatasetReaderError("CSV 必須使用 UTF-8 編碼") from exc
        return

    opener = gzip.open if source_format == "jsonl.gz" else open
    try:
        with opener(source_path, "rt", encoding="utf-8-sig") as handle:
            for row_number, line in enumerate(handle, start=1):
                if not line.strip():
                    yield SourceRecord(row_number=row_number, data=None, error_code="blank_line")
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    yield SourceRecord(row_number=row_number, data=None, error_code="invalid_json")
                    continue
                if not isinstance(row, dict):
                    yield SourceRecord(row_number=row_number, data=None, error_code="non_object_json")
                    continue
                yield SourceRecord(row_number=row_number, data=row)
    except UnicodeError as exc:
        raise DatasetReaderError("JSONL 必須使用 UTF-8 編碼") from exc


def _infer_type(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "decimal"
    text = str(value).strip()
    lowered = text.casefold()
    if lowered in {"true", "false"}:
        return "boolean"
    try:
        number = Decimal(text)
        return "integer" if number == number.to_integral_value() else "decimal"
    except (InvalidOperation, ValueError):
        return "string"


def inspect_schema(path):
    row_count = 0
    invalid_rows = 0
    type_sets = defaultdict(set)
    null_counts = defaultdict(int)
    all_fields = set()
    for record in iter_records(path):
        row_count += 1
        if record.data is None:
            invalid_rows += 1
            continue
        all_fields.update(record.data)
        for field, value in record.data.items():
            inferred = _infer_type(value)
            type_sets[field].add(inferred)
            if inferred == "null":
                null_counts[field] += 1
    fields = []
    for field in sorted(all_fields):
        types = sorted(type_sets[field] - {"null"}) or ["unknown"]
        fields.append({"name": field, "types": types, "null_count": null_counts[field]})
    return {"row_count": row_count, "invalid_rows": invalid_rows, "fields": fields}
