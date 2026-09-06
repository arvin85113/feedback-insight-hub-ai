import hashlib
import json
import os
import re
import stat
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import requests


COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PIPELINE_VERSION = "local-parquet-layer-v1"
RATING_FIELDS = ("overall", "cleanliness", "value", "location", "rooms", "sleep_quality")
CLEAN_FIELDS = (
    "hotel_id",
    "title",
    "text",
    "overall",
    "cleanliness",
    "value",
    "location",
    "rooms",
    "sleep_quality",
    "post_date",
)


class LocalDatasetError(RuntimeError):
    pass


@dataclass(frozen=True)
class LockedFile:
    relative_path: str
    size: int
    sha256: str
    url: str


@dataclass(frozen=True)
class SourceLock:
    path: Path
    payload: dict
    sha256: str
    files: tuple[LockedFile, ...]


@dataclass(frozen=True)
class DownloadedFile:
    path: Path
    size: int
    sha256: str
    cache_hit: bool


@dataclass(frozen=True)
class PreparationResult:
    manifest_path: Path
    clean_path: Path
    report_path: Path
    input_rows: int
    retained_rows: int
    duration_seconds: float
    cache_hit: bool


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(value):
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise LocalDatasetError("來源鎖定檔含不安全的相對路徑")
    return Path(*candidate.parts)


def load_source_lock(path):
    lock_path = Path(path).resolve()
    try:
        raw = lock_path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LocalDatasetError("無法讀取來源鎖定檔") from exc

    required = {
        "dataset",
        "source_revision",
        "viewer_conversion_revision",
        "config",
        "split",
        "files",
        "cleaning_version",
        "required_columns",
    }
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise LocalDatasetError("來源鎖定檔缺少必要設定")
    for key in ("source_revision", "viewer_conversion_revision"):
        if not COMMIT_PATTERN.fullmatch(str(payload[key])):
            raise LocalDatasetError(f"{key} 必須是不可變 commit SHA")
    if not payload["files"]:
        raise LocalDatasetError("來源鎖定檔未列出 Parquet 分片")

    files = []
    conversion_revision = payload["viewer_conversion_revision"]
    immutable_marker = f"/resolve/{conversion_revision}/"
    for item in payload["files"]:
        try:
            relative_path = str(item["path"])
            size = int(item["size"])
            sha256 = str(item["lfs_sha256"]).lower()
            url = str(item["url"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalDatasetError("Parquet 分片設定不完整") from exc
        _safe_relative_path(relative_path)
        if size < 1 or not SHA256_PATTERN.fullmatch(sha256):
            raise LocalDatasetError("Parquet 分片大小或可信雜湊無效")
        if not url.startswith("https://huggingface.co/") or immutable_marker not in url:
            raise LocalDatasetError("Parquet URL 未固定至轉換 commit")
        files.append(LockedFile(relative_path, size, sha256, url))

    return SourceLock(
        path=lock_path,
        payload=payload,
        sha256=hashlib.sha256(raw).hexdigest(),
        files=tuple(files),
    )


def _validate_parquet(path):
    try:
        import duckdb

        connection = duckdb.connect(":memory:")
        try:
            row_count = connection.execute(
                "SELECT count(*) FROM read_parquet(?)", [str(Path(path).resolve())]
            ).fetchone()[0]
        finally:
            connection.close()
    except Exception as exc:
        raise LocalDatasetError("Parquet 可讀性驗證失敗") from exc
    if row_count < 0:
        raise LocalDatasetError("Parquet 列數無效")
    return int(row_count)


def _validate_cached_file(destination, locked_file):
    if not destination.is_file() or destination.stat().st_size != locked_file.size:
        return None
    digest = file_sha256(destination)
    if digest != locked_file.sha256:
        raise LocalDatasetError("既有 raw 檔與來源鎖定雜湊不符；拒絕覆寫")
    _validate_parquet(destination)
    return DownloadedFile(destination, locked_file.size, digest, True)


def download_locked_file(locked_file, raw_root, *, session=None, max_retries=1):
    if max_retries != 1:
        raise LocalDatasetError("下載失敗重試次數固定為一次")
    destination = Path(raw_root).resolve() / _safe_relative_path(locked_file.relative_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    cached = _validate_cached_file(destination, locked_file)
    if cached:
        return cached

    if destination.exists():
        raise LocalDatasetError("既有 raw 檔大小不符；拒絕覆寫")
    partial = destination.with_name(destination.name + ".part")
    client = session or requests.Session()
    last_error = None

    for attempt in range(max_retries + 1):
        if partial.exists():
            partial.unlink()
        digest = hashlib.sha256()
        written = 0
        try:
            with client.get(
                locked_file.url,
                stream=True,
                timeout=(15, 60),
                headers={"User-Agent": "FeedbackInsightHub/1.0"},
            ) as response:
                response.raise_for_status()
                with partial.open("xb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > locked_file.size:
                            raise LocalDatasetError("下載內容超過鎖定大小")
                        handle.write(chunk)
                        digest.update(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            if written != locked_file.size:
                raise LocalDatasetError("下載大小與來源鎖定值不符")
            if digest.hexdigest() != locked_file.sha256:
                raise LocalDatasetError("下載雜湊與 Hub LFS 雜湊不符")
            _validate_parquet(partial)
            os.replace(partial, destination)
            return DownloadedFile(destination, written, digest.hexdigest(), False)
        except Exception as exc:
            last_error = exc
            if partial.exists():
                partial.unlink()
            if attempt >= max_retries:
                break
    raise LocalDatasetError("Parquet 下載失敗；已重試一次且未發布 raw 檔") from last_error


def download_locked_files(source_lock, raw_root, *, session=None):
    return tuple(
        download_locked_file(item, raw_root, session=session, max_retries=1)
        for item in source_lock.files
    )


def _quote_identifier(value):
    return '"' + str(value).replace('"', '""') + '"'


def _sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _atomic_json(path, payload):
    destination = Path(path)
    partial = destination.with_name(destination.name + ".part")
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if partial.exists():
        partial.unlink()
    with partial.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, destination)


def _rating_valid_expression(name):
    field = _quote_identifier(name)
    number = f"TRY_CAST({field} AS DOUBLE)"
    return f"{field} IS NOT NULL AND {number} BETWEEN 1 AND 5 AND {number} = floor({number})"


def _missing_identity_expression(name):
    field = _quote_identifier(name)
    return f"{field} IS NULL OR trim(CAST({field} AS VARCHAR)) = ''"


def build_clean_layer(source_lock, raw_files, clean_path, report_path):
    try:
        import duckdb
    except ImportError as exc:
        raise LocalDatasetError("缺少既有 duckdb 依賴") from exc

    clean_destination = Path(clean_path).resolve()
    report_destination = Path(report_path).resolve()
    for destination in (clean_destination, report_destination):
        if destination.exists():
            raise LocalDatasetError("輸出已存在但沒有有效 manifest；拒絕覆寫")
        destination.parent.mkdir(parents=True, exist_ok=True)
    clean_partial = clean_destination.with_name(clean_destination.name + ".part")
    if clean_partial.exists():
        clean_partial.unlink()

    paths_sql = ", ".join(_sql_literal(Path(item.path).resolve().as_posix()) for item in raw_files)
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET threads = 2")
        connection.execute("SET memory_limit = '1GB'")
        connection.execute(
            f"CREATE TEMP VIEW raw_data AS SELECT * FROM read_parquet([{paths_sql}], union_by_name=true)"
        )
        schema_rows = connection.execute("DESCRIBE SELECT * FROM raw_data").fetchall()
        schema = [{"name": row[0], "type": row[1], "nullable": row[2]} for row in schema_rows]
        columns = {row[0] for row in schema_rows}
        missing = set(source_lock.payload["required_columns"]) - columns
        if missing:
            raise LocalDatasetError("原始 Parquet 缺少必要欄位：" + ", ".join(sorted(missing)))

        total_rows = int(connection.execute("SELECT count(*) FROM raw_data").fetchone()[0])
        rating_report = {}
        for name in RATING_FIELDS:
            field = _quote_identifier(name)
            valid = _rating_valid_expression(name)
            missing_count, invalid_count, minimum, maximum = connection.execute(
                f"""
                SELECT
                  count(*) FILTER (WHERE {field} IS NULL),
                  count(*) FILTER (WHERE {field} IS NOT NULL AND NOT ({valid})),
                  min(TRY_CAST({field} AS DOUBLE)),
                  max(TRY_CAST({field} AS DOUBLE))
                FROM raw_data
                """
            ).fetchone()
            distribution = {
                str(int(value)): int(count)
                for value, count in connection.execute(
                    f"SELECT CAST({field} AS INTEGER), count(*) FROM raw_data WHERE {valid} GROUP BY 1 ORDER BY 1"
                ).fetchall()
            }
            rating_report[name] = {
                "missing": int(missing_count),
                "invalid": int(invalid_count),
                "minimum": minimum,
                "maximum": maximum,
                "valid_distribution": distribution,
            }

        maximum_text_length = int(source_lock.payload.get("maximum_text_length", 50000))
        text_stats = connection.execute(
            """
            SELECT
              count(*) FILTER (WHERE text IS NULL),
              count(*) FILTER (WHERE text IS NOT NULL AND trim(CAST(text AS VARCHAR)) = ''),
              count(*) FILTER (WHERE text IS NOT NULL AND length(CAST(text AS VARCHAR)) > ?),
              min(length(CAST(text AS VARCHAR))) FILTER (WHERE text IS NOT NULL),
              max(length(CAST(text AS VARCHAR))) FILTER (WHERE text IS NOT NULL),
              avg(length(CAST(text AS VARCHAR))) FILTER (WHERE text IS NOT NULL)
            FROM raw_data
            """,
            [maximum_text_length],
        ).fetchone()
        date_stats = connection.execute(
            """
            SELECT
              count(*) FILTER (WHERE post_date IS NULL),
              count(*) FILTER (WHERE post_date IS NOT NULL AND TRY_CAST(post_date AS TIMESTAMP) IS NULL),
              min(TRY_CAST(post_date AS TIMESTAMP)),
              max(TRY_CAST(post_date AS TIMESTAMP)),
              count(*) FILTER (WHERE TRY_CAST(post_date AS TIMESTAMP) < TIMESTAMP '1900-01-01'),
              count(*) FILTER (WHERE TRY_CAST(post_date AS TIMESTAMP) > CAST(? AS TIMESTAMP))
            FROM raw_data
            """,
            [source_lock.payload["source_revision_date"]],
        ).fetchone()
        hotel_stats = connection.execute(
            """
            WITH counts AS (
              SELECT hotel_id, count(*) AS row_count
              FROM raw_data WHERE hotel_id IS NOT NULL GROUP BY hotel_id
            )
            SELECT
              (SELECT count(*) FROM raw_data WHERE hotel_id IS NULL),
              count(*), min(row_count), max(row_count), avg(row_count), median(row_count)
            FROM counts
            """
        ).fetchone()
        identity_missing = {
            name: int(
                connection.execute(
                    f"SELECT count(*) FROM raw_data WHERE {_missing_identity_expression(name)}"
                ).fetchone()[0]
            )
            for name in ("hotel_id", "user_id", "post_date")
        }

        exclusion_case = f"""
          CASE
            WHEN overall IS NULL THEN 'missing_overall'
            WHEN NOT ({_rating_valid_expression('overall')}) THEN 'invalid_overall'
            WHEN text IS NULL OR trim(CAST(text AS VARCHAR)) = '' THEN 'missing_text'
            WHEN length(CAST(text AS VARCHAR)) > {maximum_text_length} THEN 'text_too_long'
            WHEN {_missing_identity_expression('hotel_id')}
              OR {_missing_identity_expression('user_id')}
              OR {_missing_identity_expression('post_date')} THEN 'missing_identity_component'
            WHEN TRY_CAST(post_date AS TIMESTAMP) IS NULL THEN 'invalid_post_date'
            ELSE NULL
          END
        """
        connection.execute(
            f"CREATE TEMP TABLE staged AS SELECT *, {exclusion_case} AS __exclusion_reason FROM raw_data"
        )
        exclusion_counts = {
            reason: int(count)
            for reason, count in connection.execute(
                "SELECT __exclusion_reason, count(*) FROM staged WHERE __exclusion_reason IS NOT NULL GROUP BY 1 ORDER BY 1"
            ).fetchall()
        }
        base_excluded = sum(exclusion_counts.values())

        source_key_values = ", ".join(
            f"COALESCE(CAST({_quote_identifier(name)} AS VARCHAR), '<NULL>')"
            for name in ("hotel_id", "user_id", "post_date")
        )
        row_hash_values = ", ".join(
            f"COALESCE(CAST({_quote_identifier(name)} AS VARCHAR), '<NULL>')"
            for name in sorted(columns)
        )
        connection.execute(
            f"""
            CREATE TEMP TABLE eligible AS
            SELECT *,
              sha256(to_json(list_value({source_key_values}))) AS __source_identity_sha256,
              sha256(to_json(list_value({row_hash_values}))) AS __raw_row_sha256
            FROM staged WHERE __exclusion_reason IS NULL
            """
        )
        connection.execute(
            """
            CREATE TEMP TABLE key_summary AS
            SELECT __source_identity_sha256, count(*) AS row_count,
                   count(DISTINCT __raw_row_sha256) AS distinct_content_count
            FROM eligible GROUP BY __source_identity_sha256
            """
        )
        duplicate_summary = connection.execute(
            """
            SELECT
              count(*) FILTER (WHERE row_count > 1),
              coalesce(sum(row_count) FILTER (WHERE row_count > 1), 0),
              count(*) FILTER (WHERE row_count > 1 AND distinct_content_count = 1),
              coalesce(sum(row_count - 1) FILTER (WHERE row_count > 1 AND distinct_content_count = 1), 0),
              count(*) FILTER (WHERE distinct_content_count > 1),
              coalesce(sum(row_count) FILTER (WHERE distinct_content_count > 1), 0)
            FROM key_summary
            """
        ).fetchone()
        candidate_duplicate_keys, candidate_duplicate_rows, exact_duplicate_keys, exact_removed, conflict_keys, conflict_rows = map(int, duplicate_summary)

        clean_select = [
            "hotel_id",
            "title",
            "text",
            "CAST(overall AS INTEGER) AS overall",
        ]
        for name in RATING_FIELDS[1:]:
            clean_select.append(
                f"CASE WHEN {_rating_valid_expression(name)} THEN CAST({_quote_identifier(name)} AS INTEGER) ELSE NULL END AS {_quote_identifier(name)}"
            )
        clean_select.extend(
            [
                "TRY_CAST(post_date AS TIMESTAMP) AS post_date",
                "__source_identity_sha256 AS source_identity_sha256",
                "length(CAST(text AS VARCHAR)) AS review_length",
            ]
        )
        clean_sql = f"""
          WITH retained AS (
            SELECT e.*,
                   row_number() OVER (
                     PARTITION BY e.__source_identity_sha256 ORDER BY e.__raw_row_sha256
                   ) AS __duplicate_rank
            FROM eligible e
            JOIN key_summary k USING (__source_identity_sha256)
            WHERE k.distinct_content_count = 1
          )
          SELECT {', '.join(clean_select)}
          FROM retained WHERE __duplicate_rank = 1
        """
        connection.execute(
            f"COPY ({clean_sql}) TO {_sql_literal(clean_partial.as_posix())} (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        clean_rows = int(connection.execute(f"SELECT count(*) FROM ({clean_sql})").fetchone()[0])
        reconciliation = clean_rows + base_excluded + exact_removed + conflict_rows
        if reconciliation != total_rows:
            raise LocalDatasetError("清理計數無法與輸入列數對帳")

        clean_schema = [
            {"name": row[0], "type": row[1], "nullable": row[2]}
            for row in connection.execute(
                f"DESCRIBE SELECT * FROM read_parquet({_sql_literal(clean_partial.as_posix())})"
            ).fetchall()
        ]
        if "user_id" in {item["name"] for item in clean_schema}:
            raise LocalDatasetError("clean schema 不得包含 user_id")

        report = {
            "report_version": PIPELINE_VERSION,
            "dataset": source_lock.payload["dataset"],
            "source_revision": source_lock.payload["source_revision"],
            "viewer_conversion_revision": source_lock.payload["viewer_conversion_revision"],
            "config": source_lock.payload["config"],
            "split": source_lock.payload["split"],
            "input_rows": total_rows,
            "raw_schema": schema,
            "ratings": rating_report,
            "text": {
                "null": int(text_stats[0]),
                "blank": int(text_stats[1]),
                "over_limit": int(text_stats[2]),
                "minimum_length": text_stats[3],
                "maximum_length": text_stats[4],
                "average_length": float(text_stats[5]) if text_stats[5] is not None else None,
            },
            "post_date": {
                "null": int(date_stats[0]),
                "unparseable": int(date_stats[1]),
                "minimum": date_stats[2].isoformat() if date_stats[2] else None,
                "maximum": date_stats[3].isoformat() if date_stats[3] else None,
                "before_1900": int(date_stats[4]),
                "after_source_revision_date": int(date_stats[5]),
            },
            "identity_component_missing": identity_missing,
            "candidate_key": {
                "definition": "SHA-256 of hotel_id + user_id + post_date; user_id is used only inside controlled local processing.",
                "duplicate_keys": candidate_duplicate_keys,
                "rows_in_duplicate_keys": candidate_duplicate_rows,
                "exact_duplicate_keys": exact_duplicate_keys,
                "exact_duplicate_rows_removed": exact_removed,
                "conflicting_keys": conflict_keys,
                "conflicting_rows_excluded": conflict_rows,
            },
            "hotel_id": {
                "null": int(hotel_stats[0]),
                "distinct": int(hotel_stats[1]),
                "rows_per_hotel_minimum": int(hotel_stats[2]) if hotel_stats[2] is not None else None,
                "rows_per_hotel_maximum": int(hotel_stats[3]) if hotel_stats[3] is not None else None,
                "rows_per_hotel_average": float(hotel_stats[4]) if hotel_stats[4] is not None else None,
                "rows_per_hotel_median": float(hotel_stats[5]) if hotel_stats[5] is not None else None,
                "semantic_evidence": source_lock.payload.get("source_card_field_evidence", {}),
            },
            "cleaning": {
                "version": source_lock.payload["cleaning_version"],
                "rules": {
                    "excluded": "missing/invalid overall; missing/blank/over-limit text; missing identity component; unparseable post_date",
                    "optional_ratings": "missing values remain null; illegal non-null values are set to null and counted",
                    "duplicates": "one exact row retained per stable key; all same-key/different-content conflicts excluded",
                    "text": "not translated, rewritten, generated, completed, or truncated",
                },
                "excluded_by_reason": exclusion_counts,
                "base_excluded_rows": base_excluded,
                "retained_rows": clean_rows,
                "reconciliation": {
                    "input": total_rows,
                    "retained": clean_rows,
                    "base_excluded": base_excluded,
                    "exact_duplicate_rows_removed": exact_removed,
                    "conflicting_rows_excluded": conflict_rows,
                    "sum": reconciliation,
                },
            },
            "clean_schema": clean_schema,
            "privacy_notice": "user_id is absent from clean rows, but review text may contain personal data; clean data is not claimed to be fully anonymous.",
        }
        _atomic_json(report_destination, report)
        os.replace(clean_partial, clean_destination)
        return report
    except Exception:
        if clean_partial.exists():
            clean_partial.unlink()
        raise
    finally:
        connection.close()


def _restrict_local_root(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        try:
            current_user = subprocess.run(
                ["whoami"], check=True, capture_output=True, text=True
            ).stdout.strip()
            subprocess.run(
                [
                    "icacls",
                    str(root),
                    "/inheritance:r",
                    "/grant:r",
                    f"{current_user}:(OI)(CI)F",
                    "/grant:r",
                    "*S-1-5-18:(OI)(CI)F",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise LocalDatasetError("無法限制 raw 本機資料目錄權限") from exc
    else:
        root.chmod(stat.S_IRWXU)


def _cached_manifest(source_lock, root):
    manifest_path = root / "manifest" / "dataset-manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["source_lock_sha256"] != source_lock.sha256:
            return None
        for artifact in manifest["artifacts"]:
            path = root / artifact["path"]
            if not path.is_file() or path.stat().st_size != artifact["size"]:
                return None
            if file_sha256(path) != artifact["sha256"]:
                return None
        return PreparationResult(
            manifest_path=manifest_path,
            clean_path=root / manifest["clean_path"],
            report_path=root / manifest["report_path"],
            input_rows=int(manifest["counts"]["input_rows"]),
            retained_rows=int(manifest["counts"]["retained_rows"]),
            duration_seconds=0.0,
            cache_hit=True,
        )
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        return None


def prepare_local_dataset(source_lock_path, output_root, *, session=None, restrict_permissions=True):
    started = time.perf_counter()
    source_lock = load_source_lock(source_lock_path)
    root = Path(output_root).resolve()
    if restrict_permissions:
        _restrict_local_root(root)
    else:
        root.mkdir(parents=True, exist_ok=True)

    cached = _cached_manifest(source_lock, root)
    if cached:
        return cached
    manifest_path = root / "manifest" / "dataset-manifest.json"
    clean_path = root / "clean" / f"tripadvisor-clean-{source_lock.payload['source_revision'][:12]}.parquet"
    report_path = root / "report" / f"full-validation-{source_lock.payload['source_revision'][:12]}.json"
    if manifest_path.exists() or clean_path.exists() or report_path.exists():
        raise LocalDatasetError("既有產物未通過 manifest 完整性驗證；拒絕覆寫")

    raw_files = download_locked_files(source_lock, root / "raw", session=session)
    report = build_clean_layer(source_lock, raw_files, clean_path, report_path)
    artifacts = []
    for item in (*raw_files, DownloadedFile(clean_path, clean_path.stat().st_size, file_sha256(clean_path), False)):
        artifacts.append(
            {
                "path": item.path.relative_to(root).as_posix(),
                "size": item.path.stat().st_size,
                "sha256": item.sha256,
            }
        )
    artifacts.append(
        {
            "path": report_path.relative_to(root).as_posix(),
            "size": report_path.stat().st_size,
            "sha256": file_sha256(report_path),
        }
    )
    duration = time.perf_counter() - started
    manifest = {
        "manifest_version": 1,
        "pipeline_version": PIPELINE_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_lock_sha256": source_lock.sha256,
        "source": {
            "dataset": source_lock.payload["dataset"],
            "source_revision": source_lock.payload["source_revision"],
            "viewer_conversion_revision": source_lock.payload["viewer_conversion_revision"],
            "config": source_lock.payload["config"],
            "split": source_lock.payload["split"],
            "files": [
                {
                    "path": item.relative_path,
                    "size": item.size,
                    "trusted_lfs_sha256": item.sha256,
                }
                for item in source_lock.files
            ],
        },
        "cleaning_version": source_lock.payload["cleaning_version"],
        "counts": {
            "input_rows": report["input_rows"],
            "retained_rows": report["cleaning"]["retained_rows"],
            "base_excluded_rows": report["cleaning"]["base_excluded_rows"],
            "exact_duplicate_rows_removed": report["candidate_key"]["exact_duplicate_rows_removed"],
            "conflicting_rows_excluded": report["candidate_key"]["conflicting_rows_excluded"],
            "excluded_by_reason": report["cleaning"]["excluded_by_reason"],
        },
        "artifacts": artifacts,
        "clean_path": clean_path.relative_to(root).as_posix(),
        "report_path": report_path.relative_to(root).as_posix(),
        "duration_seconds": round(duration, 3),
        "integrity_note": "Local SHA-256 values verify local bytes against the Hub LFS digest where provided; a self-computed hash alone is not provenance proof.",
        "privacy_notice": report["privacy_notice"],
    }
    _atomic_json(manifest_path, manifest)
    return PreparationResult(
        manifest_path=manifest_path,
        clean_path=clean_path,
        report_path=report_path,
        input_rows=report["input_rows"],
        retained_rows=report["cleaning"]["retained_rows"],
        duration_seconds=duration,
        cache_hit=False,
    )
