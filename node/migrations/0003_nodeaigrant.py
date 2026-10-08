from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("node", "0002_localdatasetlocation"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [migrations.CreateModel(name="NodeAIGrant", fields=[
        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
        ("identity", models.JSONField()),
        ("confirmation_uuid", models.UUIDField(unique=True)),
        ("max_calls", models.PositiveSmallIntegerField(default=6)),
        ("calls_started", models.PositiveSmallIntegerField(default=0)),
        ("in_flight", models.BooleanField(default=False)),
        ("status", models.CharField(default="queued", max_length=16)),
        ("error_code", models.CharField(blank=True, max_length=64)),
        ("expires_at", models.DateTimeField()),
        ("created_at", models.DateTimeField(auto_now_add=True)),
        ("actor", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
        ("job", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="node_ai_grant", to="feedback.analysisjob")),
    ])]
