from django.db import migrations, models


def create_local_model_config(apps, schema_editor):
    APIConfig = apps.get_model('core', 'APIConfig')
    defaults = {
        'deepseek': ('https://api.deepseek.com', 'deepseek-v4-flash'),
        'kimi': ('https://api.moonshot.cn', 'moonshot-v1-8k'),
    }
    for service_name, (base_url, model_name) in defaults.items():
        APIConfig.objects.filter(service_name=service_name, base_url='').update(
            base_url=base_url,
            model_name=model_name,
        )
    APIConfig.objects.update_or_create(
        service_name='local',
        defaults={
            'api_key': '',
            'base_url': 'http://192.168.0.182:8000',
            'model_name': 'XKY-AI',
            'is_active': True,
            'test_success': False,
            'last_test_time': None,
        },
    )


class Migration(migrations.Migration):
    dependencies = [('core', '0017_expense_monthly_ledger_fields')]

    operations = [
        migrations.AlterField(
            model_name='apiconfig',
            name='service_name',
            field=models.CharField(
                choices=[
                    ('deepseek', 'DeepSeek'),
                    ('kimi', 'Kimi (Moonshot)'),
                    ('local', '本地模型'),
                ],
                max_length=20,
                unique=True,
                verbose_name='AI服务',
            ),
        ),
        migrations.AlterField(
            model_name='apiconfig',
            name='api_key',
            field=models.TextField(blank=True, verbose_name='API密钥'),
        ),
        migrations.AddField(
            model_name='apiconfig',
            name='base_url',
            field=models.URLField(
                blank=True,
                help_text='填写服务根地址、/v1 地址或完整的 chat/completions 地址。',
                max_length=500,
                verbose_name='服务地址',
            ),
        ),
        migrations.AddField(
            model_name='apiconfig',
            name='model_name',
            field=models.CharField(blank=True, max_length=100, verbose_name='模型名称'),
        ),
        migrations.RunPython(create_local_model_config, migrations.RunPython.noop),
    ]
