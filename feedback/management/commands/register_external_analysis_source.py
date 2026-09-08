"""Register one verified local external dataset version for a survey."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from feedback.analysis_sources import register_external_dataset_version
from feedback.models import Survey


class Command(BaseCommand):
    help = (
        "將已驗證的本機 external manifest 登錄為問卷的作用中分析來源；"
        "只寫入來源版本與工作狀態，不上傳或匯入 Parquet 列資料。"
    )

    def add_arguments(self, parser):
        parser.add_argument("--survey", required=True, help="目標問卷 slug")
        parser.add_argument("--manifest", required=True, help="本機 dataset-manifest.json 路徑")
        parser.add_argument("--mapping", required=True, help="版本化 mapping JSON 路徑")
        parser.add_argument("--dry-run", action="store_true", help="只驗證並顯示將登錄的版本，不寫資料庫")

    @staticmethod
    def _read_json(path, label):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CommandError(f"找不到{label}") from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CommandError(f"無法讀取{label}") from exc

    @staticmethod
    def _hash_file(path):
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise CommandError("無法讀取清理後 Parquet") from exc
        return digest.hexdigest()

    def handle(self, *args, **options):
        survey = Survey.objects.filter(slug=options["survey"]).first()
        if survey is None:
            raise CommandError("找不到目標問卷")
        manifest_path = Path(options["manifest"]).expanduser().resolve()
        mapping_path = Path(options["mapping"]).expanduser().resolve()
        manifest = self._read_json(manifest_path, "資料清單")
        mapping = self._read_json(mapping_path, "欄位設定")
        try:
            source = manifest["source"]
            clean_relative = manifest["clean_path"]
            artifact = next(item for item in manifest["artifacts"] if item["path"] == clean_relative)
            source_ref = str(source["dataset"])
            source_revision = str(source["source_revision"])
            cleaning_version = str(manifest["cleaning_version"])
            content_sha256 = str(artifact["sha256"])
            content_size = int(artifact["size"])
            row_count = int(manifest["counts"]["retained_rows"])
            mapping_version = str(mapping["mapping_version"])
        except (KeyError, StopIteration, TypeError, ValueError) as exc:
            raise CommandError("資料清單或 mapping 缺少外部版本必要欄位") from exc
        if mapping.get("dataset", {}).get("name") != source_ref:
            raise CommandError("mapping 與 manifest 的資料集名稱不一致")
        if mapping.get("dataset", {}).get("version") != source_revision:
            raise CommandError("mapping 與 manifest 的來源 revision 不一致")
        if mapping_path.stem != mapping_path.stem.lower():
            raise CommandError("mapping 檔名必須使用小寫識別碼")
        source_version = f"{source_revision}:{cleaning_version}:{content_sha256}"
        dataset_root = manifest_path.parent.parent.resolve()
        clean_path = (dataset_root / str(clean_relative)).resolve()
        if not clean_path.is_relative_to(dataset_root) or not clean_path.is_file():
            raise CommandError("找不到 manifest 指定的清理後 Parquet")
        if clean_path.stat().st_size != content_size:
            raise CommandError("清理後 Parquet 大小與 manifest 不一致")
        if self._hash_file(clean_path) != content_sha256:
            raise CommandError("清理後 Parquet SHA-256 與 manifest 不一致")
        schema_basis = {
            "mapping_version": mapping_version,
            "questions": mapping.get("questions", []),
            "required_fields": mapping.get("huggingface_preparation", {}).get("required_fields", []),
        }
        schema_sha256 = hashlib.sha256(
            json.dumps(schema_basis, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        source_latest_at = None
        report_relative = manifest.get("report_path")
        if report_relative:
            report_path = (dataset_root / str(report_relative)).resolve()
            if report_path.is_relative_to(dataset_root) and report_path.is_file():
                report = self._read_json(report_path, "全量驗證報告")
                source_latest_at = parse_datetime(str((report.get("post_date") or {}).get("maximum") or ""))
                if source_latest_at is not None and timezone.is_naive(source_latest_at):
                    source_latest_at = timezone.make_aware(source_latest_at)
        provenance = {
            "dataset_url": mapping.get("dataset", {}).get("source_url", ""),
            "license_name": mapping.get("dataset", {}).get("license", ""),
            "viewer_conversion_revision": source.get("viewer_conversion_revision", ""),
            "config": mapping.get("huggingface_preparation", {}).get("config", ""),
            "split": mapping.get("huggingface_preparation", {}).get("split", ""),
        }
        message = (
            f"問卷={survey.slug} source={source_ref} version={source_version} "
            f"rows={row_count} mapping={mapping_path.stem}@{mapping_version}"
        )
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("DRY-RUN " + message))
            return
        _, version, job, changed = register_external_dataset_version(
            survey.pk,
            source_ref=source_ref,
            source_version=source_version,
            source_revision=source_revision,
            cleaning_version=cleaning_version,
            content_sha256=content_sha256,
            schema_sha256=schema_sha256,
            mapping_key=mapping_path.stem,
            mapping_version=mapping_version,
            row_count=row_count,
            source_latest_at=source_latest_at,
            provenance=provenance,
        )
        status = "已切換作用中版本" if changed else "作用中版本未變"
        job_label = str(job.pk) if job else "未排程"
        self.stdout.write(self.style.SUCCESS(f"{status}；version_id={version.pk} job={job_label}；{message}"))
