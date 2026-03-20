from django import forms
from decimal import Decimal
from .models import Project, ProjectAnalysis

PROJECT_STATUS_CHOICES = [
    ('未立项', '未立项'),
    ('申报', '申报'),
    ('立项', '立项'),
    ('已立项', '已立项'),
    ('在研', '在研'),
    ('延期', '延期'),
    ('结题', '结题'),
    ('终止', '终止'),
]

class ProjectForm(forms.ModelForm):
    # 添加自定义的研究内容字段，支持手动编辑
    research_content_manual = forms.CharField(
        widget=forms.Textarea(attrs={
            'rows': 8, 
            'cols': 80,
            'placeholder': '此内容将自动从"研究内容分析"结果同步，您也可以手动编辑...'
        }), 
        required=False,
        label="主要研究内容",
        help_text="可以手动编辑或来自AI分析结果"
    )
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 如果存在实例，初始化手动编辑字段
        if self.instance and self.instance.pk:
            self.fields['research_content_manual'].initial = self.instance.research_content
        # 隐藏原始的research_content字段
        self.fields['research_content'].widget = forms.HiddenInput()
        if 'start_year' in self.fields:
            self.fields['start_year'].required = False
        # 使用日期选择器
        date_fields = ['start_date', 'planned_end_date', 'extension_date', 'actual_completion_date']
        for field_name in date_fields:
            if field_name in self.fields:
                self.fields[field_name].widget = forms.DateInput(attrs={'type': 'date'})
        if 'status' in self.fields:
            status_choices = list(PROJECT_STATUS_CHOICES)
            status_values = {value for value, _ in status_choices}
            try:
                existing_statuses = (
                    Project.objects.values_list('status', flat=True)
                    .distinct()
                    .order_by('status')
                )
                for status in existing_statuses:
                    if status and status not in status_values:
                        status_choices.append((status, status))
                        status_values.add(status)
            except Exception:
                pass
            if getattr(self.instance, 'status', None) and self.instance.status not in status_values:
                status_choices.append((self.instance.status, self.instance.status))
            status_choices = [('', '未选择')] + status_choices
            self.fields['status'].widget = forms.Select(choices=status_choices)
            self.fields['status'].choices = status_choices

    def clean(self):
        cleaned_data = super().clean()
        start_year = cleaned_data.get('start_year')
        start_date = cleaned_data.get('start_date')
        if not start_year and start_date:
            cleaned_data['start_year'] = start_date.year
        if not cleaned_data.get('start_year') and not start_date:
            self.add_error('start_date', '请选择开始日期')
        external = cleaned_data.get('external_funding') or Decimal('0')
        institute = cleaned_data.get('institute_funding') or Decimal('0')
        unit = cleaned_data.get('unit_funding') or Decimal('0')
        if external or institute or unit:
            cleaned_data['total_budget'] = external + institute + unit
        return cleaned_data
    
    def save(self, commit=True):
        instance = super().save(commit=False)
        # 将手动编辑的内容保存到research_content字段
        if self.cleaned_data.get('research_content_manual'):
            instance.research_content = self.cleaned_data['research_content_manual']
        if commit:
            instance.save()
        return instance

    class Meta:
        model = Project
        fields = '__all__'
        exclude = ['directory_path'] # 目录路径由系统在后台自动管理，不应由用户直接编辑
