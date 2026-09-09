from django.db import migrations, models
import django.db.models.deletion


CATEGORIES = (
    ('ip', '知识产权类', 10),
    ('academic_output', '学术产出类', 20),
    ('technical_standard', '技术标准', 30),
    ('equipment_process', '装备、工艺类', 40),
)

INDICATORS = (
    ('ip', '发明专利', '项', '受理/授权', 10),
    ('ip', '实用新型专利', '项', '受理/授权', 20),
    ('ip', '外观专利', '项', '受理/授权', 30),
    ('ip', '软件著作权', '项', '受理/授权', 40),
    ('academic_output', '核心期刊/SCI/EI论文', '项', '录用证明或发表', 10),
    ('academic_output', '学术专著', '项', '正式出版物', 20),
    ('technical_standard', '企业级/团体/地方/行业/国家标准', '项', '公布/成稿', 10),
    ('technical_standard', '企业级/省部级/国家级工法', '项', '公布/成稿', 20),
    ('equipment_process', '样机', '套', '试制', 10),
    ('equipment_process', '图纸', '套', '图纸归档', 20),
)


def seed_metrics_catalog(apps, schema_editor):
    MetricsCategory = apps.get_model('core', 'MetricsCategory')
    MetricIndicatorDefinition = apps.get_model('core', 'MetricIndicatorDefinition')
    categories = {}
    for code, name, sort_order in CATEGORIES:
        category, _ = MetricsCategory.objects.update_or_create(
            code=code,
            defaults={'name': name, 'sort_order': sort_order, 'is_active': True},
        )
        categories[code] = category
    for code, name, unit, assessment_method, sort_order in INDICATORS:
        MetricIndicatorDefinition.objects.update_or_create(
            name=name,
            defaults={
                'category': categories[code],
                'unit': unit,
                'assessment_method': assessment_method,
                'sort_order': sort_order,
                'is_active': True,
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0014_correct_metrics_catalog_classification'),
    ]

    operations = [
        migrations.CreateModel(
            name='MetricsCategory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=20, unique=True, verbose_name='分类代码')),
                ('name', models.CharField(max_length=100, unique=True, verbose_name='指标大类')),
                ('sort_order', models.PositiveIntegerField(default=0, verbose_name='排序')),
                ('is_active', models.BooleanField(default=True, verbose_name='启用')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
            ],
            options={
                'verbose_name': '指标大类配置',
                'verbose_name_plural': '指标大类配置',
                'ordering': ['sort_order', 'id'],
            },
        ),
        migrations.CreateModel(
            name='MetricIndicatorDefinition',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=255, unique=True, verbose_name='具体指标')),
                ('unit', models.CharField(blank=True, max_length=20, verbose_name='量化单位')),
                ('assessment_method', models.CharField(blank=True, max_length=255, verbose_name='交付/佐证形式')),
                ('sort_order', models.PositiveIntegerField(default=0, verbose_name='排序')),
                ('is_active', models.BooleanField(default=True, verbose_name='启用')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
                ('category', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='indicators', to='core.metricscategory', verbose_name='指标大类')),
            ],
            options={
                'verbose_name': '具体指标配置',
                'verbose_name_plural': '具体指标配置',
                'ordering': ['category__sort_order', 'sort_order', 'id'],
            },
        ),
        migrations.RunPython(seed_metrics_catalog, migrations.RunPython.noop),
    ]
