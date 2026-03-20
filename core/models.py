from django.db import models
from django.core.exceptions import ValidationError
from cryptography.fernet import Fernet
import base64
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
    
    managing_unit = models.CharField(max_length=100, blank=True, verbose_name="归口单位")

    LEVEL_CHOICES = [('国家级', '国家级'), ('省部级', '省部级'), ('公司级', '公司级')]
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES, verbose_name="课题级别")

    TYPE_CHOICES = [('应用研究', '应用研究'), ('试验发展', '试验发展'), ('全自筹课题', '全自筹课题')]
    project_type = models.CharField(max_length=20, choices=TYPE_CHOICES, verbose_name="课题类型")

    ROLE_CHOICES = [('牵头', '牵头'), ('参与', '参与')]
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, verbose_name="参与角色")

    # 时间与状态
    start_year = models.IntegerField(verbose_name="开始年份", db_index=True)
    status = models.CharField(max_length=50, verbose_name="课题状态", db_index=True)
    contact_person = models.CharField(max_length=50, blank=True, verbose_name="课题联系人")
    project_lead = models.CharField(max_length=50, blank=True, verbose_name="课题负责人")
    start_date = models.DateField(null=True, blank=True, verbose_name="开始日期")
    planned_end_date = models.DateField(null=True, blank=True, verbose_name="计划结束日期")
    extension_date = models.DateField(null=True, blank=True, verbose_name="延期时间")
    actual_completion_date = models.DateField(null=True, blank=True, verbose_name="实际结题时间")

    # 经费预算 (使用DecimalField以保证精度)
    total_budget = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="总预算(万元)")
    external_funding = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="外部专项经费(万元)")
    institute_funding = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="院自筹经费(万元)")
    unit_funding = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="所属单位自筹经费(万元)")

    # 描述性内容
    research_content = models.TextField(blank=True, verbose_name="主要研究内容")
    remarks = models.TextField(blank=True, verbose_name="备注")

    # 文件系统关联
    directory_path = models.CharField(max_length=512, verbose_name="物理目录相对路径")

    # 自动维护字段
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

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
    analysis_result = models.TextField(verbose_name="分析结果")
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
    
    CATEGORY_CHOICES = [
        ('technical', '技术成果指标'),
        ('academic', '学术成果指标'),
        ('standard', '标准制定指标'),
        ('talent', '人才培养指标'),
        ('economic', '经济效益指标'),
    ]
    
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
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending',
        verbose_name="完成状态"
    )
    deadline = models.DateField(null=True, blank=True, verbose_name="截止时间")
    notes = models.TextField(blank=True, verbose_name="备注")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")
    
    def __str__(self):
        return f"{self.get_category_display()} - {self.item_name}"
    
    class Meta:
        verbose_name = "产出指标项目"
        verbose_name_plural = "产出指标项目"
        ordering = ['category', 'item_name']


class ExpenseImport(models.Model):
    source_file = models.CharField(max_length=512, verbose_name="数据来源文件")
    sheet_name = models.CharField(max_length=50, default="Sheet2", verbose_name="工作表")
    file_mtime = models.DateTimeField(null=True, blank=True, verbose_name="文件更新时间")
    threshold = models.FloatField(default=0.85, verbose_name="匹配阈值")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="导入时间")

    def __str__(self):
        return f"{self.source_file} @ {self.created_at:%Y-%m-%d %H:%M}"

    class Meta:
        verbose_name = "支出数据导入"
        verbose_name_plural = "支出数据导入"
        ordering = ['-created_at']


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
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='expense_snapshots',
        verbose_name="关联课题"
    )
    project_name = models.CharField(max_length=255, verbose_name="课题名称")
    company_name = models.CharField(max_length=100, blank=True, verbose_name="公司名称")
    matched_description = models.TextField(blank=True, verbose_name="匹配描述")
    match_score = models.FloatField(null=True, blank=True, verbose_name="匹配分数")
    total_expense = models.DecimalField(max_digits=14, decimal_places=2, default=0, verbose_name="累计支出")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="建立时间")

    def __str__(self):
        return f"{self.project_name} - {self.total_expense}"

    class Meta:
        verbose_name = "支出快照"
        verbose_name_plural = "支出快照"
        ordering = ['-created_at']


class APIConfig(models.Model):
    """AI API配置管理"""
    
    SERVICE_CHOICES = [
        ('deepseek', 'DeepSeek'),
        ('kimi', 'Kimi (Moonshot)'),
    ]
    
    service_name = models.CharField(
        max_length=20, 
        choices=SERVICE_CHOICES,
        unique=True,
        verbose_name="AI服务"
    )
    api_key = models.TextField(verbose_name="API密钥")
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
