from django.db import migrations


def convert(apps, schema_editor):
    from feedback.schema_conversion import convert_definitions

    convert_definitions(apps, using=schema_editor.connection.alias)


class Migration(migrations.Migration):
    dependencies = [
        ("feedback", "0023_survey_builder_fields"),
        ("cloudapi", "0003_publishedresultrecord"),
    ]

    operations = [migrations.RunPython(convert, migrations.RunPython.noop)]
