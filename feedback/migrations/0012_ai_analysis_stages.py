import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("feedback", "0011_survey_ai_report_snapshot"),
    ]

    operations = [
        migrations.CreateModel(
            name="SurveyAIAnalysisStage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "stage_type",
                    models.CharField(
                        choices=[
                            ("statistics", "統計分析"),
                            ("text", "文字洞察"),
                            ("synthesis", "綜合營運決策"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("generating", "產生中"),
                            ("succeeded", "成功"),
                            ("failed", "失敗"),
                        ],
                        max_length=16,
                    ),
                ),
                ("input_hash", models.CharField(max_length=64)),
                ("schema_version", models.CharField(max_length=32)),
                ("prompt_version", models.CharField(max_length=32)),
                ("model_name", models.CharField(max_length=100)),
                ("revision", models.PositiveIntegerField(default=1)),
                ("input_manifest", models.JSONField(default=dict)),
                ("output_json", models.JSONField(blank=True, null=True)),
                ("error_code", models.CharField(blank=True, max_length=64)),
                ("generation_ms", models.PositiveIntegerField(blank=True, null=True)),
                ("token_metrics", models.JSONField(default=dict)),
                ("generated_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "reused_from",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="reuse_rows",
                        to="feedback.surveyaianalysisstage",
                    ),
                ),
                (
                    "snapshot",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="analysis_stages",
                        to="feedback.surveyaireportsnapshot",
                    ),
                ),
            ],
            options={
                "db_table": "feedback_survey_ai_analysis_stages",
                "indexes": [
                    models.Index(
                        fields=["snapshot", "stage_type", "status", "created_at"],
                        name="fb_ai_stage_lookup_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=(
                            "snapshot",
                            "stage_type",
                            "input_hash",
                            "schema_version",
                            "prompt_version",
                            "model_name",
                            "revision",
                        ),
                        name="uniq_ai_stage_revision",
                    )
                ],
            },
        ),
        migrations.AddField(
            model_name="improvementupdate",
            name="source_ai_analysis_stage",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="created_improvements",
                to="feedback.surveyaianalysisstage",
            ),
        ),
        migrations.AddField(
            model_name="improvementupdate",
            name="source_ai_draft_id",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="improvementupdate",
            name="source_ai_metadata",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="improvementupdate",
            name="source_evidence_refs",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="improvementupdate",
            constraint=models.UniqueConstraint(
                fields=("source_ai_analysis_stage", "source_ai_draft_id"),
                name="uniq_improvement_ai_draft",
            ),
        ),
    ]
