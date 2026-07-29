from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('core', '0009_alter_project_level'),
    ]

    operations = [
        migrations.CreateModel(
            name='OperationLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('username', models.CharField(blank=True, max_length=150, verbose_name='用户名快照')),
                ('method', models.CharField(max_length=10, verbose_name='请求方法')),
                ('path', models.CharField(max_length=500, verbose_name='请求路径')),
                ('action_name', models.CharField(blank=True, max_length=100, verbose_name='操作名称')),
                ('status_code', models.PositiveSmallIntegerField(verbose_name='响应状态码')),
                ('ip_address', models.GenericIPAddressField(blank=True, null=True, verbose_name='IP地址')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='操作时间')),
                ('user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='research_operation_logs', to=settings.AUTH_USER_MODEL, verbose_name='操作用户')),
            ],
            options={
                'verbose_name': '操作日志',
                'verbose_name_plural': '操作日志',
                'ordering': ['-created_at'],
            },
        ),
    ]
