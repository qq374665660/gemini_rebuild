from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_normalize_project_status'),
    ]

    operations = [
        migrations.AlterField(
            model_name='project',
            name='level',
            field=models.CharField(
                choices=[
                    ('国家级', '国家级'),
                    ('省部级', '省部级'),
                    ('地市级', '地市级'),
                    ('公司级', '公司级'),
                ],
                max_length=20,
                verbose_name='课题级别',
            ),
        ),
    ]
