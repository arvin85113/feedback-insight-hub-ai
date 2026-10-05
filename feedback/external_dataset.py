"""Read-only validation shared by source-registration entry points.

No database, downloads, row exports or remote dataset code. This checks the
recorded clean artifact's integrity, not the upstream publisher's rights.
"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from django.utils import timezone
from django.utils.dateparse import parse_datetime


class ExternalDatasetInvalid(ValueError):
    pass


def file_hash(path):
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ExternalDatasetInvalid("無法讀取資料檔") from exc
    return digest.hexdigest()


def read_json(path, label):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExternalDatasetInvalid(f"無法讀取{label}") from exc
    if not isinstance(value, dict):
        raise ExternalDatasetInvalid(f"{label}必須是物件")
    return value


@dataclass(frozen=True)
class VerifiedExternalDataset:
    manifest_path: Path
    mapping_path: Path
    manifest_sha256: str
    mapping_sha256: str
    registration: dict


def validate_external_dataset(manifest_path, mapping_path):
    manifest_path = Path(manifest_path).expanduser().resolve()
    mapping_path = Path(mapping_path).expanduser().resolve()
    # Hash the exact bytes parsed; changes between validation and worker use fail closed.
    manifest_hash, mapping_hash = file_hash(manifest_path), file_hash(mapping_path)
    manifest = read_json(manifest_path, "資料清單")
    mapping = read_json(mapping_path, "欄位設定")
    try:
        source = manifest["source"]
        clean_relative = manifest["clean_path"]
        artifact = next(a for a in manifest["artifacts"] if a["path"] == clean_relative)
        source_ref = str(source["dataset"])
        revision = str(source["source_revision"])
        cleaning = str(manifest["cleaning_version"])
        content_hash = str(artifact["sha256"])
        size = artifact["size"]
        rows = manifest["counts"]["retained_rows"]
        mapping_version = str(mapping["mapping_version"])
        if (not isinstance(size, int) or isinstance(size, bool) or size < 1
                or not isinstance(rows, int) or isinstance(rows, bool) or rows < 1):
            raise ValueError()
    except (KeyError, StopIteration, TypeError, ValueError) as exc:
        raise ExternalDatasetInvalid("資料清單或 mapping 缺少有效版本、大小或筆數") from exc
    if mapping.get("dataset", {}).get("name") != source_ref:
        raise ExternalDatasetInvalid("mapping 與 manifest 的資料集名稱不一致")
    if mapping.get("dataset", {}).get("version") != revision:
        raise ExternalDatasetInvalid("mapping 與 manifest 的來源 revision 不一致")
    if mapping_path.stem != mapping_path.stem.lower():
        raise ExternalDatasetInvalid("mapping 檔名必須使用小寫識別碼")
    root = manifest_path.parent.parent.resolve()
    clean_path = (root / str(clean_relative)).resolve()
    if not clean_path.is_relative_to(root) or not clean_path.is_file():
        raise ExternalDatasetInvalid("找不到 manifest 指定的清理後 Parquet")
    if clean_path.stat().st_size != size or file_hash(clean_path) != content_hash:
        raise ExternalDatasetInvalid("清理後 Parquet 大小或 SHA-256 與 manifest 不一致")
    schema_basis = {
        "mapping_version": mapping_version,
        "questions": mapping.get("questions", []),
        "required_fields": mapping.get("huggingface_preparation", {}).get("required_fields", []),
    }
    schema_hash = hashlib.sha256(json.dumps(
        schema_basis, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    latest = None
    if manifest.get("report_path"):
        report_path = (root / str(manifest["report_path"])).resolve()
        if not report_path.is_relative_to(root):
            raise ExternalDatasetInvalid("報告路徑超出資料集目錄")
        if report_path.is_file():
            report = read_json(report_path, "全量驗證報告")
            latest = parse_datetime(str((report.get("post_date") or {}).get("maximum") or ""))
            if latest is not None and timezone.is_naive(latest):
                latest = timezone.make_aware(latest)
    if file_hash(manifest_path) != manifest_hash or file_hash(mapping_path) != mapping_hash:
        raise ExternalDatasetInvalid("驗證途中 manifest 或 mapping 已變更")
    registration = dict(
        source_ref=source_ref, source_version=f"{revision}:{cleaning}:{content_hash}",
        source_revision=revision, cleaning_version=cleaning, content_sha256=content_hash,
        schema_sha256=schema_hash, mapping_key=mapping_path.stem, mapping_version=mapping_version,
        row_count=rows, source_latest_at=latest,
        provenance={
            "dataset_url": mapping.get("dataset", {}).get("source_url", ""),
            "license_name": mapping.get("dataset", {}).get("license", ""),
            "viewer_conversion_revision": source.get("viewer_conversion_revision", ""),
            "config": mapping.get("huggingface_preparation", {}).get("config", ""),
            "split": mapping.get("huggingface_preparation", {}).get("split", ""),
        },
    )
    return VerifiedExternalDataset(manifest_path, mapping_path, manifest_hash, mapping_hash, registration)
