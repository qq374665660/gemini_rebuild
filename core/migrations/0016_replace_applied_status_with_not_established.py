from django.db import migrations, models


def replace_applied_status(apps, schema_editor):
    Project = apps.get_model('core', 'Project')
    Project.objects.filter(status='申报').update(status='未立项')


def restore_applied_status(apps, schema_editor):
    Project = apps.get_model('core', 'Project')
    Project.objects.filter(status='未立项').update(status='申报')


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0015_metrics_catalog_configuration'),
    ]

    operations = [
        migrations.RunPython(replace_applied_status, restore_applied_status),
        migrations.AlterField(
            model_name='project',
            name='status',
            field=models.CharField(
                choices=[
                    ('未立项', '未立项'),
                    ('在研', '在研'),
                    ('延期', '延期'),
                    ('结题', '结题'),
                    ('终止', '终止'),
                ],
                db_index=True,
                max_length=50,
                verbose_name='课题状态',
            ),
        ),
    ]
