from django.db import models
from django.db.utils import OperationalError, ProgrammingError
from django.conf import settings
from django.core.exceptions import ValidationError
from cryptography.fernet import Fernet
import base64
from decimal import Decimal
import os

class Project(models.Model):
    """科研课题信息表"""

    # 核心身份标识
    project_id = models.CharField(
        max_length=100,
        unique=True,
        primary_key=True,
        verbose_name="课题编号"
    )
    name = models.CharField(max_length=255, verbose_name="课题名称")

    # 分类与归属
    OWNERSHIP_CHOICES = [('西勘院', '西勘院'), ('地下空间', '地下空间')]
    ownership = models.CharField(max_length=20, choices=OWNERSHIP_CHOICES, verbose_name="课题归属")

    FUNDING_CATEGORY_CHOICES = [
        ('special', '专项经费课题'),
        ('self_funded', '企业全自筹课题'),
    ]
    funding_category = models.CharField(
        max_length=20,
        choices=FUNDING_CATEGORY_CHOICES,
        default='special',
        db_index=True,
        verbose_name="经费管理类别",
    )
    
    managing_unit = models.CharField(max_length=100, blank=True, verbose_name="归口单位")

    LEVEL_CHOICES = [
        ('国家级', '国家级'),
        ('省部级', '省部级'),
        ('地市级', '地市级'),
        ('公司级', '公司级'),
    ]
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES, verbose_name="课题级别")

    TYPE_CHOICES = [('应用研究', '应用研究'), ('试验发展', '试验发展'), ('全自筹课题', '全自筹课题')]
    project_type = models.CharField(max_length=20, choices=TYPE_CHOICES, verbose_name="课题类型")

    ROLE_CHOICES = [('牵头', '牵头'), ('参与', '参与')]
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, verbose_name="参与角色")

    STATUS_CHOICES = [
        ('未立项', '未立项'),
        ('在研', '在研'),
        ('延期', '延期'),
        ('结题', '结题'),
        ('终止', '终止'),
    ]
    STATUS_ALIASES = {
        '申报': '未立项',
        '立项': '在研',
        '已立项': '在研',
        '进行中': '在研',
        '执行中': '在研',
        '完成': '结题',
        '已完成': '结题',
    }

    # 时间与状态
    start_year = models.IntegerField(verbose_name="开始年份", db_index=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, verbose_name="课题状态", db_index=True)
    contact_person = models.CharField(max_length=50, blank=True, verbose_name="课题联系人")
    project_lead = models.CharField(max_length=50, blank=True, verbose_name="课题负责人")
    start_date = models.DateField(null=True, blank=True, verbose_name="开始日期")
    planned_end_date = models.DateField(null=True, blank=True, verbose_name="计划结束日期")
    extension_date = models.DateField(null=True, blank=True, verbose_name="延期时间")
    actual_completion_date = models.DateField(null=True, blank=True, verbose_name="实际结题时间")

    # 经费预算 (使用DecimalField以保证精度；金额单位统一为万元)
    total_budget = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="总预算")
    external_funding = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="外部专项")
    institute_funding = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="院专项")
    unit_funding = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="单位自筹")

    # 描述性内容
    research_content = models.TextField(blank=True, verbose_name="主要研究内容")
    remarks = models.TextField(blank=True, verbose_name="备注")

    # 文件系统关联
    directory_path = models.CharField(max_length=512, verbose_name="物理目录相对路径")

    # 自动维护字段
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def normalize_status(cls, value):
        """将历史状态归并为当前允许的五种状态。"""
        status = str(value or '').strip()
        status = cls.STATUS_ALIASES.get(status, status)
        valid_statuses = {choice[0] for choice in cls.STATUS_CHOICES}
        return status if status in valid_statuses else ''

    BUDGET_PART_FIELDS = ('external_funding', 'institute_funding', 'unit_funding')

    def clean(self):
        super().clean()
        parts = [getattr(self, field) for field in self.BUDGET_PART_FIELDS]
        if self.total_budget is None or any(part is None for part in parts):
            # 历史总表和任务书常只给部分经费，缺项时不臆造总预算。
            return
        expected = sum(parts, Decimal('0'))
        if abs(self.total_budget - expected) > Decimal('0.01'):
            labels = '、'.join(
                self._meta.get_field(field).verbose_name for field in self.BUDGET_PART_FIELDS
            )
            raise ValidationError({
                'total_budget': ValidationError(
                    f'总预算应等于{labels}之和（{expected} 万元），当前为 {self.total_budget} 万元。'
                )
            })

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "科研课题"
        verbose_name_plural = "科研课题"
        ordering = ['-start_year', 'project_id']


class ProjectAnalysis(models.Model):
    """课题分析结果表"""
    
    ANALYSIS_TYPE_CHOICES = [
        ('research_content', '研究内容分析'),
        ('output_metrics', '产出指标分析'),
    ]
    
    project = models.ForeignKey(
        Project, 
        on_delete=models.CASCADE, 
        related_name='analyses',
        verbose_name="关联课题"
    )
    analysis_type = models.CharField(
        max_length=20, 
        choices=ANALYSIS_TYPE_CHOICES,
        verbose_name="分析类型"
    )
    file_name = models.CharField(max_length=255, verbose_name="分析文件名")
    file_size = models.IntegerField(null=True, blank=True, verbose_name="文件大小(字节)")
    source_relative_path = models.CharField(
        max_length=512,
        blank=True,
        verbose_name="课题文件相对路径",
        help_text="分析来源位于课题文件目录内时记录其相对路径。",
    )
    analysis_result = models.TextField(verbose_name="分析结果")
    structured_data = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="结构化分析数据",
        help_text="保留从任务书中提取的目标、研究内容、指标及进度等结构化数据。",
    )
    confidence_score = models.FloatField(
        null=True, blank=True, 
        verbose_name="置信度分数",
        help_text="AI分析结果的置信度，0-1之间"
    )
    processing_time = models.FloatField(
        null=True, blank=True,
        verbose_name="处理时间(秒)"
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")
    
    def __str__(self):
        return f"{self.project.name} - {self.get_analysis_type_display()}"
    
    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # 如果是研究内容分析，自动更新Project的research_content字段
        if self.analysis_type == 'research_content':
            self.project.research_content = self.analysis_result
            self.project.save(update_fields=['research_content'])
    
    class Meta:
        verbose_name = "课题分析"
        verbose_name_plural = "课题分析"
        ordering = ['-created_at']
        unique_together = ['project', 'analysis_type']  # 每个课题每种分析类型只能有一个结果


class MetricsItem(models.Model):
    """产出指标项目表"""

    INDICATOR_CATALOG = (
        {
            'category': 'ip',
            'category_label': '知识产权类',
            'item_name': '发明专利',
            'unit': '项',
            'assessment_method': '受理/授权',
        },
        {
            'category': 'ip',
            'category_label': '知识产权类',
            'item_name': '实用新型专利',
            'unit': '项',
            'assessment_method': '受理/授权',
        },
        {
            'category': 'ip',
            'category_label': '知识产权类',
            'item_name': '外观专利',
            'unit': '项',
            'assessment_method': '受理/授权',
        },
        {
            'category': 'ip',
            'category_label': '知识产权类',
            'item_name': '软件著作权',
            'unit': '项',
            'assessment_method': '受理/授权',
        },
        {
            'category': 'academic_output',
            'category_label': '学术产出类',
            'item_name': '核心期刊/SCI/EI论文',
            'unit': '项',
            'assessment_method': '录用证明或发表',
        },
        {
            'category': 'academic_output',
            'category_label': '学术产出类',
            'item_name': '学术专著',
            'unit': '项',
            'assessment_method': '正式出版物',
        },
        {
            'category': 'technical_standard',
            'category_label': '技术标准',
            'item_name': '企业级/团体/地方/行业/国家标准',
            'unit': '项',
            'assessment_method': '公布/成稿',
        },
        {
            'category': 'technical_standard',
            'category_label': '技术标准',
            'item_name': '企业级/省部级/国家级工法',
            'unit': '项',
            'assessment_method': '公布/成稿',
        },
        {
            'category': 'equipment_process',
            'category_label': '装备、工艺类',
            'item_name': '样机',
            'unit': '套',
            'assessment_method': '试制',
        },
        {
            'category': 'equipment_process',
            'category_label': '装备、工艺类',
            'item_name': '图纸',
            'unit': '套',
            'assessment_method': '图纸归档',
        },
    )
    
    CATEGORY_CHOICES = [
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
    ]

    @classmethod
    def get_indicator_catalog(cls, include_inactive=False):
        try:
            queryset = MetricIndicatorDefinition.objects.select_related('category')
            if not include_inactive:
                queryset = queryset.filter(is_active=True, category__is_active=True)
            return tuple({
                'category': item.category.code,
                'category_label': item.category.name,
                'item_name': item.name,
                'unit': item.unit,
                'assessment_method': item.assessment_method,
            } for item in queryset)
        except (OperationalError, ProgrammingError):
            return cls.INDICATOR_CATALOG

    @classmethod
    def get_catalog_item(cls, item_name, include_inactive=False):
        normalized_name = str(item_name or '').strip()
        return next(
            (
                item for item in cls.get_indicator_catalog(include_inactive=include_inactive)
                if item['item_name'] == normalized_name
            ),
            None,
        )

    @classmethod
    def get_category_label_map(cls):
        labels = dict(cls.CATEGORY_CHOICES)
        try:
            labels.update(MetricsCategory.objects.values_list('code', 'name'))
        except (OperationalError, ProgrammingError):
            pass
        return labels

    def get_configured_category_display(self):
        return self.get_category_label_map().get(self.category, self.category)
    
    STATUS_CHOICES = [
        ('pending', '待完成'),
        ('in_progress', '进行中'),
        ('completed', '已完成'),
        ('cancelled', '已取消'),
    ]
    
    analysis = models.ForeignKey(
        ProjectAnalysis,
        on_delete=models.CASCADE,
        related_name='metrics_items',
        verbose_name="关联分析"
    )
    category = models.CharField(
        max_length=20,
        choices=CATEGORY_CHOICES,
        verbose_name="指标分类"
    )
    item_name = models.CharField(max_length=255, verbose_name="指标项目名称")
    target_value = models.CharField(max_length=100, verbose_name="目标值")
    current_value = models.CharField(max_length=100, blank=True, verbose_name="当前值")
    assessment_method = models.CharField(max_length=255, blank=True, verbose_name="考核方式")
    planned_period = models.CharField(max_length=100, blank=True, verbose_name="计划时间")
    source_section = models.CharField(max_length=100, blank=True, verbose_name="任务书来源章节")
    source_page = models.CharField(max_length=50, blank=True, verbose_name="任务书来源页码")
    responsible_person = models.CharField(max_length=100, blank=True, verbose_name="责任人")
    progress_percent = models.PositiveSmallIntegerField(default=0, verbose_name="完成进度(%)")
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending',
        verbose_name="完成状态"
    )
    deadline = models.DateField(null=True, blank=True, verbose_name="截止时间")
    actual_completion_date = models.DateField(null=True, blank=True, verbose_name="实际完成日期")
    sort_order = models.PositiveIntegerField(default=0, verbose_name="排序")
    extraction_source = models.CharField(
        max_length=20,
        default='manual',
        choices=[('ai', 'AI提取'), ('manual', '手动录入'), ('legacy', '历史数据')],
        verbose_name="数据来源",
    )
    notes = models.TextField(blank=True, verbose_name="备注")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")
    
    def __str__(self):
        return f"{self.get_category_display()} - {self.item_name}"
    
    class Meta:
        verbose_name = "产出指标项目"
        verbose_name_plural = "产出指标项目"
        ordering = ['sort_order', 'category', 'item_name']


class MetricsCategory(models.Model):
    """可在系统设置中维护的指标大类。"""

    code = models.CharField(max_length=20, unique=True, verbose_name='分类代码')
    name = models.CharField(max_length=100, unique=True, verbose_name='指标大类')
    sort_order = models.PositiveIntegerField(default=0, verbose_name='排序')
    is_active = models.BooleanField(default=True, verbose_name='启用')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = '指标大类配置'
        verbose_name_plural = '指标大类配置'
        ordering = ['sort_order', 'id']


class MetricIndicatorDefinition(models.Model):
    """可配置的具体指标、量化单位及交付形式。"""

    category = models.ForeignKey(
        MetricsCategory,
        on_delete=models.PROTECT,
        related_name='indicators',
        verbose_name='指标大类',
    )
    name = models.CharField(max_length=255, unique=True, verbose_name='具体指标')
    unit = models.CharField(max_length=20, blank=True, verbose_name='量化单位')
    assessment_method = models.CharField(max_length=255, blank=True, verbose_name='交付/佐证形式')
    sort_order = models.PositiveIntegerField(default=0, verbose_name='排序')
    is_active = models.BooleanField(default=True, verbose_name='启用')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    def __str__(self):
        return f'{self.category.name} - {self.name}'

    class Meta:
        verbose_name = '具体指标配置'
        verbose_name_plural = '具体指标配置'
        ordering = ['category__sort_order', 'sort_order', 'id']


class MetricEvidence(models.Model):
    """指标完成情况所关联的课题文件。"""

    metric = models.ForeignKey(
        MetricsItem,
        on_delete=models.CASCADE,
        related_name='evidence_files',
        verbose_name="关联指标",
    )
    relative_path = models.CharField(max_length=512, verbose_name="课题文件相对路径")
    display_name = models.CharField(max_length=255, verbose_name="文件名")
    note = models.CharField(max_length=255, blank=True, verbose_name="佐证说明")
    created_by = models.ForeignKey(
        'auth.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='metric_evidence_links',
        verbose_name="关联人",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="关联时间")

    def __str__(self):
        return f"{self.metric.item_name} - {self.display_name}"

    class Meta:
        verbose_name = "指标佐证文件"
        verbose_name_plural = "指标佐证文件"
        ordering = ['created_at', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['metric', 'relative_path'],
                name='unique_metric_evidence_path',
            ),
        ]


class ExpenseImport(models.Model):
    source_file = models.CharField(max_length=512, verbose_name="数据来源文件")
    sheet_name = models.CharField(max_length=50, default="Sheet2", verbose_name="工作表")
    original_filename = models.CharField(max_length=255, blank=True, verbose_name="原始文件名")
    file_sha256 = models.CharField(max_length=64, blank=True, db_index=True, verbose_name="文件哈希")
    format_version = models.CharField(max_length=50, default="legacy", db_index=True, verbose_name="数据口径版本")
    file_mtime = models.DateTimeField(null=True, blank=True, verbose_name="文件更新时间")
    threshold = models.FloatField(default=0.85, verbose_name="匹配阈值")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="导入时间")

    def __str__(self):
        return f"{self.source_file} @ {self.created_at:%Y-%m-%d %H:%M}"

    class Meta:
        verbose_name = "支出数据导入"
        verbose_name_plural = "支出数据导入"
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['format_version', 'file_sha256'],
                condition=~models.Q(file_sha256=''),
                name='unique_expense_import_file_hash',
            ),
        ]


class ExpenseMapping(models.Model):
    description_text = models.TextField(verbose_name="原始描述")
    normalized_text = models.CharField(max_length=512, unique=True, verbose_name="标准化描述")
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='expense_mappings',
        verbose_name="匹配课题"
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    def __str__(self):
        return f"{self.description_text[:20]} -> {self.project.name}"

    class Meta:
        verbose_name = "手工映射规则"
        verbose_name_plural = "手工映射规则"
        ordering = ['-updated_at']


class ExpenseSnapshot(models.Model):
    import_log = models.ForeignKey(
        ExpenseImport,
        on_delete=models.CASCADE,
        related_name='snapshots',
        verbose_name="导入批次"
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='expense_snapshots',
        verbose_name="关联课题",
    )
    funding_category = models.CharField(
        max_length=20,
        choices=Project.FUNDING_CATEGORY_CHOICES,
        default='special',
        db_index=True,
        verbose_name="经费管理类别",
    )
    project_name = models.CharField(max_length=255, verbose_name="课题名称")
    company_name = models.CharField(max_length=100, blank=True, verbose_name="公司名称")
    matched_description = models.TextField(blank=True, verbose_name="匹配描述")
    match_score = models.FloatField(null=True, blank=True, verbose_name="匹配分数")
    total_expense = models.DecimalField(max_digits=18, decimal_places=4, default=0, verbose_name="累计支出")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="建立时间")

    def __str__(self):
        return f"{self.project_name} - {self.total_expense}"

    class Meta:
        verbose_name = "支出快照"
        verbose_name_plural = "支出快照"
        ordering = ['-created_at']


class SpecialLedgerImport(models.Model):
    """专项经费面板使用的一次台账导入。"""

    LEDGER_TYPE_CHOICES = [
        ('external', '外部立项课题'),
        ('institute', '院自主立项课题'),
    ]
    FORMAT_VERSION = 'special-ledger-v1'

    ledger_type = models.CharField(
        max_length=20,
        choices=LEDGER_TYPE_CHOICES,
        db_index=True,
        verbose_name="台账类别",
    )
    source_file = models.CharField(max_length=512, verbose_name="数据来源文件")
    original_filename = models.CharField(max_length=255, blank=True, verbose_name="原始文件名")
    file_sha256 = models.CharField(max_length=64, blank=True, db_index=True, verbose_name="文件哈希")
    format_version = models.CharField(max_length=50, default=FORMAT_VERSION, verbose_name="数据口径版本")
    sheet_name = models.CharField(max_length=50, default='汇总表', verbose_name="工作表")
    row_total = models.PositiveIntegerField(default=0, verbose_name="台账课题数")
    matched_total = models.PositiveIntegerField(default=0, verbose_name="已关联课题数")
    ambiguous_total = models.PositiveIntegerField(default=0, verbose_name="待确认课题数")
    ignored_total = models.PositiveIntegerField(default=0, verbose_name="忽略课题数")
    totals = models.JSONField(default=dict, blank=True, verbose_name="汇总金额(万元)")
    ignored_detail = models.JSONField(default=list, blank=True, verbose_name="忽略课题清单")
    file_mtime = models.DateTimeField(null=True, blank=True, verbose_name="文件更新时间")
    created_by = models.ForeignKey(
        'auth.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='special_ledger_imports',
        verbose_name="导入人",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="导入时间")

    def __str__(self):
        return f"{self.get_ledger_type_display()}台账 @ {self.created_at:%Y-%m-%d %H:%M}"

    class Meta:
        verbose_name = "专项经费台账导入"
        verbose_name_plural = "专项经费台账导入"
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['ledger_type', 'file_sha256'],
                condition=~models.Q(file_sha256=''),
                name='unique_special_ledger_file_hash',
            ),
        ]


class SpecialLedgerRow(models.Model):
    """台账汇总表里的一行课题，金额已折算为万元。"""

    MATCH_STATE_CHOICES = [
        ('exact', '同名唯一匹配'),
        ('manual', '人工指定'),
        ('ambiguous', '系统重名待确认'),
        ('unmatched', '系统无此课题'),
    ]

    import_log = models.ForeignKey(
        SpecialLedgerImport,
        on_delete=models.CASCADE,
        related_name='rows',
        verbose_name="导入批次",
    )
    ledger_type = models.CharField(
        max_length=20,
        choices=SpecialLedgerImport.LEDGER_TYPE_CHOICES,
        db_index=True,
        verbose_name="台账类别",
    )
    row_number = models.PositiveIntegerField(default=0, verbose_name="源表行号")
    sequence = models.CharField(max_length=50, blank=True, verbose_name="台账序号")
    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='special_ledger_rows',
        verbose_name="关联课题",
    )
    ledger_name = models.CharField(max_length=255, verbose_name="台账课题名称")
    owning_unit = models.CharField(max_length=100, blank=True, verbose_name="归属单位")
    funder = models.CharField(max_length=100, blank=True, verbose_name="经费来源")
    ledger_status = models.CharField(max_length=50, blank=True, verbose_name="台账研发进度")
    principal = models.CharField(max_length=50, blank=True, verbose_name="课题负责人")
    start_text = models.CharField(max_length=50, blank=True, verbose_name="开始时间")
    end_text = models.CharField(max_length=50, blank=True, verbose_name="结束时间")
    match_state = models.CharField(
        max_length=20,
        choices=MATCH_STATE_CHOICES,
        default='unmatched',
        db_index=True,
        verbose_name="匹配情况",
    )
    contract_total = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True, verbose_name="课题合同经费")
    contract_allocated = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True, verbose_name="归属本院合同经费")
    received_amount = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True, verbose_name="已到账经费")
    approved_budget = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True, verbose_name="预算额度")
    executed_total = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True, verbose_name="总执行额度")
    remaining_amount = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True, verbose_name="可支出经费")
    year_disposable = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True, verbose_name="本年度可支配经费")
    year_budget = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True, verbose_name="本年预算额")
    year_executed = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True, verbose_name="本年执行额度")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="建立时间")

    @property
    def unreceived_amount(self):
        """外部课题已立项但尚未拨付到院的经费。

        基数用「归属院/地下空间课题合同经费」而非「课题合同经费」：院只拿自己那一份，
        用整课题合同额会把兄弟单位的份额算成欠款。院自主课题由院内预算额度直接下达，
        不存在到账环节，返回 None 让页面显示“-”而不是 0。
        """
        if self.ledger_type != 'external':
            return None
        if self.contract_allocated is None or self.received_amount is None:
            return None
        return self.contract_allocated - self.received_amount

    def __str__(self):
        return f"{self.ledger_name} - {self.executed_total}"

    class Meta:
        verbose_name = "专项经费台账明细"
        verbose_name_plural = "专项经费台账明细"
        ordering = ['ledger_type', 'row_number']


class SpecialLedgerAssignment(models.Model):
    """台账行与系统课题的人工对应关系，用于消解重名歧义。"""

    ledger_type = models.CharField(max_length=20, choices=SpecialLedgerImport.LEDGER_TYPE_CHOICES, verbose_name="台账类别")
    ledger_name = models.CharField(max_length=255, verbose_name="台账课题名称")
    normalized_name = models.CharField(max_length=255, verbose_name="标准化名称")
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='special_ledger_assignments',
        verbose_name="对应课题",
    )
    note = models.CharField(max_length=255, blank=True, verbose_name="备注")
    created_by = models.ForeignKey(
        'auth.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='special_ledger_assignments',
        verbose_name="确认人",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="确认时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    def __str__(self):
        return f"{self.ledger_name} -> {self.project.name}"

    class Meta:
        verbose_name = "专项台账对应关系"
        verbose_name_plural = "专项台账对应关系"
        ordering = ['-updated_at']
        constraints = [
            models.UniqueConstraint(
                fields=['ledger_type', 'normalized_name'],
                name='unique_special_ledger_assignment',
            ),
        ]


class APIConfig(models.Model):
    """AI API配置管理"""
    
    SERVICE_CHOICES = [
        ('deepseek', 'DeepSeek'),
        ('kimi', 'Kimi (Moonshot)'),
        ('local', '本地模型'),
    ]
    
    service_name = models.CharField(
        max_length=20, 
        choices=SERVICE_CHOICES,
        unique=True,
        verbose_name="AI服务"
    )
    api_key = models.TextField(blank=True, verbose_name="API密钥")
    base_url = models.URLField(
        max_length=500,
        blank=True,
        verbose_name="服务地址",
        help_text="填写服务根地址、/v1 地址或完整的 chat/completions 地址。",
    )
    model_name = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="模型名称",
    )
    is_active = models.BooleanField(default=True, verbose_name="启用状态")
    test_success = models.BooleanField(default=False, verbose_name="测试通过")
    last_test_time = models.DateTimeField(null=True, blank=True, verbose_name="最后测试时间")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")
    
    # 加密密钥（应该从环境变量或配置文件读取）
    _encryption_key = None
    
    @classmethod
    def get_encryption_key(cls):
        if cls._encryption_key is None:
            # 尝试从环境变量获取加密密钥
            key = os.getenv('API_ENCRYPTION_KEY')
            if not key:
                # 如果没有环境变量，生成一个基于机器的唯一密钥
                import hashlib
                import platform
                machine_info = f"{platform.node()}-{platform.machine()}-{platform.processor()}"
                key_bytes = hashlib.sha256(machine_info.encode()).digest()[:32]
                key = base64.urlsafe_b64encode(key_bytes).decode()
            cls._encryption_key = key
        return cls._encryption_key
    
    def encrypt_api_key(self, raw_key):
        """加密API密钥"""
        if not raw_key:
            return ''
        
        try:
            fernet = Fernet(self.get_encryption_key())
            encrypted_key = fernet.encrypt(raw_key.encode())
            return base64.urlsafe_b64encode(encrypted_key).decode()
        except Exception:
            # 如果加密失败，回退到明文存储（不推荐但确保功能可用）
            return raw_key
    
    def decrypt_api_key(self):
        """解密API密钥"""
        if not self.api_key:
            return ''
        
        try:
            encrypted_data = base64.urlsafe_b64decode(self.api_key.encode())
            fernet = Fernet(self.get_encryption_key())
            decrypted_key = fernet.decrypt(encrypted_data).decode()
            return decrypted_key
        except Exception:
            # 如果解密失败，假设是明文存储的
            return self.api_key
    
    def set_api_key(self, raw_key):
        """设置API密钥（自动加密）"""
        self.api_key = self.encrypt_api_key(raw_key)
    
    def get_api_key(self):
        """获取解密后的API密钥"""
        return self.decrypt_api_key()
    
    def clean(self):
        """验证API密钥格式"""
        if self.api_key:
            try:
                # 尝试解密并验证API密钥
                decrypted = self.decrypt_api_key()
                if decrypted:
                    decrypted = decrypted.strip()
                    # 验证密钥长度和基本格式
                    if len(decrypted) < 10:
                        raise ValidationError({
                            'api_key': 'API密钥长度不足，请检查密钥是否完整'
                        })
                    # 对于某些服务，验证特定格式
                    if self.service_name in ['deepseek', 'kimi'] and not decrypted.startswith('sk-'):
                        raise ValidationError({
                            'api_key': 'API密钥格式不正确，应该以 sk- 开头'
                        })
                elif self.service_name in ['deepseek', 'kimi']:
                    raise ValidationError({'api_key': '该服务必须配置API密钥'})
            except ValidationError:
                raise  # 重新抛出验证错误
            except Exception:
                # 解密失败或其他异常时，进行基本长度检查
                if len(self.api_key) < 10:
                    raise ValidationError({
                        'api_key': 'API密钥格式无效或长度不足'
                    })
    
    def __str__(self):
        status = "✓" if self.is_active else "✗"
        test_status = "✓" if self.test_success else "✗"
        return f"{self.get_service_name_display()} {status} (测试:{test_status})"
    
    class Meta:
        verbose_name = "AI API配置"
        verbose_name_plural = "AI API配置"
        ordering = ['service_name']


class OperationLog(models.Model):
    """记录登录用户对系统执行的写入及敏感下载操作。"""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='research_operation_logs',
        verbose_name='操作用户',
    )
    username = models.CharField(max_length=150, blank=True, verbose_name='用户名快照')
    method = models.CharField(max_length=10, verbose_name='请求方法')
    path = models.CharField(max_length=500, verbose_name='请求路径')
    action_name = models.CharField(max_length=100, blank=True, verbose_name='操作名称')
    status_code = models.PositiveSmallIntegerField(verbose_name='响应状态码')
    ip_address = models.GenericIPAddressField(null=True, blank=True, verbose_name='IP地址')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='操作时间')

    class Meta:
        verbose_name = '操作日志'
        verbose_name_plural = '操作日志'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.username or "未知用户"} {self.method} {self.path}'
