from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("node", "0001_initial"), ("feedback", "0024_convert_survey_definitions")]

    operations = [
        migrations.CreateModel(
            name="LocalDatasetLocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("manifest_path", models.TextField()),
                ("mapping_path", models.TextField()),
                ("manifest_sha256", models.CharField(max_length=64)),
                ("mapping_sha256", models.CharField(max_length=64)),
                ("verified_at", models.DateTimeField(auto_now=True)),
                ("version", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT,
                                               related_name="local_location", to="feedback.externaldatasetversion")),
            ],
        ),
    ]
