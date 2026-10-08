"""Register a verified manifest without importing or uploading its rows."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from feedback.analysis_sources import register_external_dataset_version
from feedback.external_dataset import ExternalDatasetInvalid, validate_external_dataset
from feedback.models import Survey


class Command(BaseCommand):
    help = "驗證並登錄固定外部來源；節點模式另保存本機路徑。只寫來源與工作，不匯入列資料。"

    def add_arguments(self, parser):
        parser.add_argument("--survey", required=True, help="目標問卷 slug")
        parser.add_argument("--manifest", required=True, help="本機 dataset-manifest.json 路徑")
        parser.add_argument("--mapping", required=True, help="版本化 mapping JSON 路徑")
        parser.add_argument("--dry-run", action="store_true", help="只驗證，不寫資料庫")

    def handle(self, *args, **options):
        survey = Survey.objects.filter(slug=options["survey"]).first()
        if survey is None:
            raise CommandError("找不到目標問卷")
        try:
            verified = validate_external_dataset(options["manifest"], options["mapping"])
        except ExternalDatasetInvalid as exc:
            raise CommandError(str(exc)) from exc
        data = verified.registration
        message = (
            f"問卷={survey.slug} source={data['source_ref']} version={data['source_version']} "
            f"rows={data['row_count']} mapping={data['mapping_key']}@{data['mapping_version']}"
        )
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("DRY-RUN " + message))
            return
        if settings.IS_NODE:
            from node.datasets import register_local_dataset

            _, version, job, changed = register_local_dataset(survey.pk, verified)
        else:
            _, version, job, changed = register_external_dataset_version(survey.pk, **data)
        status = "已切換作用中版本" if changed else "作用中版本未變"
        job_label = str(job.pk) if job else "未排程"
        self.stdout.write(self.style.SUCCESS(f"{status}；version_id={version.pk} job={job_label}；{message}"))
