from django.db import migrations


def mark_existing_metrics_as_legacy(apps, schema_editor):
    MetricsItem = apps.get_model('core', 'MetricsItem')
    MetricsItem.objects.update(extraction_source='legacy')


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0011_alter_metricsitem_options_and_more'),
    ]

    operations = [
        migrations.RunPython(mark_existing_metrics_as_legacy, migrations.RunPython.noop),
    ]
