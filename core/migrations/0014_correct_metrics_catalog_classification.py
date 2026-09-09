from django.db import migrations


CATALOG_NAMES = {
    '发明专利',
    '实用新型专利',
    '外观专利',
    '软件著作权',
    '核心期刊/SCI/EI论文',
    '学术专著',
    '企业级/团体/地方/行业/国家标准',
    '企业级/省部级/国家级工法',
    '样机',
    '图纸',
}

LEGACY_CATEGORY_BY_NEW_CATEGORY = {
    'ip': 'deliverable',
    'academic_output': 'academic',
    'technical_standard': 'standard',
    'equipment_process': 'technical',
}


def normalize_name(value):
    return ''.join(str(value or '').split())


def correct_overclassified_metrics(apps, schema_editor):
    MetricsItem = apps.get_model('core', 'MetricsItem')
    new_categories = tuple(LEGACY_CATEGORY_BY_NEW_CATEGORY)
    queryset = MetricsItem.objects.filter(category__in=new_categories).only(
        'id', 'item_name', 'category', 'source_section'
    )
    for metric in queryset.iterator():
        if normalize_name(metric.item_name) in CATALOG_NAMES:
            continue
        source_section = metric.source_section or ''
        if '进度' in source_section or '计划' in source_section:
            category = 'milestone'
        elif '经济' in source_section or '社会' in source_section or '效益' in source_section:
            category = 'benefit'
        else:
            category = LEGACY_CATEGORY_BY_NEW_CATEGORY[metric.category]
        MetricsItem.objects.filter(pk=metric.pk).update(category=category)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0013_metrics_indicator_catalog'),
    ]

    operations = [
        migrations.RunPython(correct_overclassified_metrics, migrations.RunPython.noop),
    ]
