from django.db import migrations, models


def mark_legacy_self_funded_projects(apps, schema_editor):
    Project = apps.get_model('core', 'Project')
    Project.objects.filter(project_type='全自筹课题').update(funding_category='self_funded')


class Migration(migrations.Migration):
    dependencies = [('core', '0018_apiconfig_local_model')]

    operations = [
        migrations.AddField(
            model_name='project',
            name='funding_category',
            field=models.CharField(
                choices=[('special', '专项经费课题'), ('self_funded', '企业全自筹课题')],
                db_index=True,
                default='special',
                max_length=20,
                verbose_name='经费管理类别',
            ),
        ),
        migrations.AddField(
            model_name='expensesnapshot',
            name='funding_category',
            field=models.CharField(
                choices=[('special', '专项经费课题'), ('self_funded', '企业全自筹课题')],
                db_index=True,
                default='special',
                max_length=20,
                verbose_name='经费管理类别',
            ),
        ),
        migrations.RunPython(mark_legacy_self_funded_projects, migrations.RunPython.noop),
    ]
