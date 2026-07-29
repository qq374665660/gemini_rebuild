from django.db import migrations, models


LEGACY_STATUS_MAP = {
    '未立项': '申报',
    '立项': '在研',
    '已立项': '在研',
    '进行中': '在研',
    '执行中': '在研',
    '完成': '结题',
    '已完成': '结题',
}


def normalize_project_statuses(apps, schema_editor):
    Project = apps.get_model('core', 'Project')
    for old_status, new_status in LEGACY_STATUS_MAP.items():
        Project.objects.filter(status=old_status).update(status=new_status)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0007_expensemapping'),
    ]

    operations = [
        migrations.RunPython(
            normalize_project_statuses,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name='project',
            name='status',
            field=models.CharField(
                choices=[
                    ('申报', '申报'),
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
