from django.db import migrations, models


CATALOG_MAP = {
    '发明专利': 'ip',
    '实用新型专利': 'ip',
    '外观专利': 'ip',
    '软件著作权': 'ip',
    '核心期刊/SCI/EI论文': 'academic_output',
    '学术专著': 'academic_output',
    '企业级/团体/地方/行业/国家标准': 'technical_standard',
    '企业级/省部级/国家级工法': 'technical_standard',
    '样机': 'equipment_process',
    '图纸': 'equipment_process',
}


def normalize_name(value):
    return ''.join(str(value or '').split())


def classify_existing_metrics(apps, schema_editor):
    MetricsItem = apps.get_model('core', 'MetricsItem')
    for metric in MetricsItem.objects.all().only('id', 'item_name', 'category').iterator():
        category = CATALOG_MAP.get(normalize_name(metric.item_name))
        if category and metric.category != category:
            MetricsItem.objects.filter(pk=metric.pk).update(category=category)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0012_mark_existing_metrics_as_legacy'),
    ]

    operations = [
        migrations.AlterField(
            model_name='metricsitem',
            name='category',
            field=models.CharField(
                choices=[
                    ('ip', '知识产权类'),
                    ('academic_output', '学术产出类'),
                    ('technical_standard', '技术标准'),
                    ('equipment_process', '装备、工艺类'),
                    ('deliverable', '任务书考核指标'),
                    ('benefit', '经济与社会效益'),
                    ('milestone', '阶段进度目标'),
                    ('technical', '技术成果指标'),
                    ('academic', '学术成果指标'),
                    ('standard', '标准制定指标'),
                    ('talent', '人才培养指标'),
                    ('economic', '经济效益指标'),
                    ('other', '其他指标'),
                ],
                max_length=20,
                verbose_name='指标分类',
            ),
        ),
        migrations.RunPython(classify_existing_metrics, migrations.RunPython.noop),
    ]
