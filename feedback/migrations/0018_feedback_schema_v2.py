import hashlib
import json
import uuid

from django.db import migrations, models
from django.utils.text import slugify


def backfill_v2_fields(apps, schema_editor):
    Question = apps.get_model("feedback", "Question")
    Submission = apps.get_model("feedback", "FeedbackSubmission")
    Answer = apps.get_model("feedback", "Answer")
    ImportedSource = apps.get_model("feedback", "ImportedSubmissionSource")

    used_codes = {}
    for question in Question.objects.order_by("survey_id", "id").iterator(chunk_size=2000):
        used = used_codes.setdefault(question.survey_id, set())
        base = slugify(question.title)[:70] or f"question-{question.pk}"
        code = base
        suffix = 2
        while code in used:
            code = f"{base[:70]}-{suffix}"
            suffix += 1
        used.add(code)
        updates = {"code": code}
        if question.kind == "scale":
            updates["data_type"] = "ordinal"
        elif question.kind == "integer":
            updates["data_type"] = "discrete"
        Question.objects.filter(pk=question.pk).update(**updates)

    for submission in Submission.objects.order_by("id").iterator(chunk_size=2000):
        Submission.objects.filter(pk=submission.pk).update(
            idempotency_key=uuid.uuid5(uuid.NAMESPACE_URL, f"feedback-submission:{submission.pk}"),
            ingested_at=submission.submitted_at,
            is_complete=Answer.objects.filter(submission_id=submission.pk).exists(),
        )

    sources = ImportedSource.objects.select_related("batch", "submission").order_by("id")
    for source in sources.iterator(chunk_size=500):
        answers = list(
            Answer.objects.filter(submission_id=source.submission_id)
            .order_by("question_id")
            .values_list("question_id", "value")
        )
        payload = {
            "answers": answers,
            "source_item_id": source.source_item_id,
            "source_timestamp": source.source_timestamp.isoformat()
            if source.source_timestamp
            else None,
            "metadata": source.metadata,
        }
        content_sha256 = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        # The legacy key included the dataset version and cannot be safely
        # reconstructed after sensitive source identifiers were discarded.
        # Keep legacy identities in a separate namespace instead of claiming
        # they are equivalent to the new cross-version record key.
        namespace = f"{source.batch.source_name}@legacy:{source.batch.source_version}"
        ImportedSource.objects.filter(pk=source.pk).update(
            source_namespace=namespace[:160],
            source_version=source.batch.source_version,
            content_sha256=content_sha256,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("feedback", "0017_datasetimportbatch_requested_limit_nullable"),
    ]

    operations = [
        migrations.AddField(
            model_name="survey",
            name="analysis_enabled",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="survey",
            name="archived_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="question",
            name="code",
            field=models.SlugField(blank=True, max_length=80, null=True),
        ),
        migrations.AddField(
            model_name="question",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="feedbacksubmission",
            name="idempotency_key",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="feedbacksubmission",
            name="ingested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="feedbacksubmission",
            name="is_complete",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="feedbacksubmission",
            name="voided_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="datasetimportbatch",
            name="conflict_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.RenameField(
            model_name="importedsubmissionsource",
            old_name="source_row_hash",
            new_name="source_record_key",
        ),
        migrations.AlterField(
            model_name="importedsubmissionsource",
            name="source_record_key",
            field=models.CharField(max_length=64),
        ),
        migrations.AddField(
            model_name="importedsubmissionsource",
            name="content_sha256",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="importedsubmissionsource",
            name="source_namespace",
            field=models.CharField(blank=True, max_length=160),
        ),
        migrations.AddField(
            model_name="importedsubmissionsource",
            name="source_version",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.RunPython(backfill_v2_fields, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="question",
            name="code",
            field=models.SlugField(max_length=80),
        ),
        migrations.AlterField(
            model_name="feedbacksubmission",
            name="idempotency_key",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name="feedbacksubmission",
            name="ingested_at",
            field=models.DateTimeField(auto_now_add=True),
        ),
        migrations.AlterField(
            model_name="importedsubmissionsource",
            name="content_sha256",
            field=models.CharField(max_length=64),
        ),
        migrations.AlterField(
            model_name="importedsubmissionsource",
            name="source_namespace",
            field=models.CharField(max_length=160),
        ),
        migrations.AlterField(
            model_name="importedsubmissionsource",
            name="source_version",
            field=models.CharField(max_length=100),
        ),
        migrations.AddConstraint(
            model_name="question",
            constraint=models.UniqueConstraint(
                fields=("survey", "code"),
                name="fb_question_survey_code_uniq",
            ),
        ),
        migrations.AddIndex(
            model_name="feedbacksubmission",
            index=models.Index(
                fields=("survey", "is_complete", "voided_at", "submitted_at"),
                name="fb_sub_analysis_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="importedsubmissionsource",
            constraint=models.UniqueConstraint(
                fields=("source_namespace", "source_record_key"),
                name="fb_import_source_record_uniq",
            ),
        ),
        migrations.AddIndex(
            model_name="importedsubmissionsource",
            index=models.Index(
                fields=("source_namespace", "source_version"),
                name="fb_import_version_idx",
            ),
        ),
    ]
