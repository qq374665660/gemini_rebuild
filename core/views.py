from django.shortcuts import render, redirect, get_object_or_404
from collections import defaultdict
from django.conf import settings
from django.urls import reverse
from django.db.models import Q, Count
from django.http import HttpResponse, Http404, FileResponse, JsonResponse, HttpResponseForbidden
from django.utils.encoding import force_str
import shutil
import os
import re
import logging
import mimetypes
import time
import tempfile
import subprocess
import uuid
import hashlib
from decimal import Decimal, InvalidOperation
from django.db import transaction
from .models import (
    Project,
    ProjectAnalysis,
    APIConfig,
    MetricsItem,
    MetricsCategory,
    MetricIndicatorDefinition,
    MetricEvidence,
    ExpenseImport,
    ExpenseSnapshot,
    ExpenseMapping,
    OperationLog,
    SpecialLedgerImport,
    SpecialLedgerRow,
    SpecialLedgerAssignment,
)
from .forms import ProjectForm
import openpyxl
from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.core.paginator import Paginator
from django.template.loader import render_to_string
import json
import requests
from django.utils import timezone
from pathlib import Path
from datetime import datetime, timedelta
from io import BytesIO
from urllib.parse import quote
from django.views.decorators.http import require_POST
from .docx_task_extractor import extract_task_docx, extract_task_pdf
from . import backup_schedule as backup_scheduler
from django.utils.dateparse import parse_date
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from .access import is_system_admin
from .analysis_parsing import parse_ai_analysis, parse_metrics_text
from .query_assistant import answer_project_question
from .ai_providers import (
    config_is_usable,
    get_model_name,
    post_chat_completion,
    provider_defaults,
    provider_payload,
    ready_configs,
)
from .expense_analysis import (
    EXPENSE_FORMAT_VERSION,
    EXPENSE_UNIT_LABEL,
    LIFETIME_LABEL,
    TARGET_COMPANIES,
    ExpenseWorkbookError,
    abbreviate_company_name,
    analyze_expense_workbook,
    clean_match_text,
    file_sha256,
    inspect_expense_workbook,
    normalized_expense_description,
    similarity_score,
)
from .special_ledger import (
    LEDGER_SPECIAL_BUDGET_FIELD,
    SpecialLedgerError,
    normalize_project_name,
    parse_special_ledger,
)

logger = logging.getLogger(__name__)

def _normalize_path(path_str):
    normalized = os.path.normpath(path_str)
    if os.name == 'nt':
        normalized = os.path.normcase(normalized)
    return normalized

def _resolve_for_compare(path_str):
    try:
        resolved = Path(path_str).expanduser().resolve(strict=False)
    except Exception:
        resolved = Path(os.path.normpath(path_str))
    return _normalize_path(str(resolved))

def _is_within_root(target_path, root_path):
    try:
        target_norm = _resolve_for_compare(target_path)
        root_norm = _resolve_for_compare(root_path)
        return os.path.commonpath([target_norm, root_norm]) == root_norm
    except Exception:
        return False

def _build_abs_path(base_dir, maybe_relative):
    if not maybe_relative:
        return str(base_dir)
    maybe_relative = str(maybe_relative)
    if os.path.isabs(maybe_relative):
        return maybe_relative
    return os.path.join(str(base_dir), maybe_relative)

def _relpath_for_tree(item_path, base_path):
    try:
        rel_path = os.path.relpath(item_path, base_path)
    except ValueError:
        rel_path = str(item_path)
    return rel_path.replace('\\', '/')

def _resolve_within_root(root, relative_path):
    """把相对路径解析到课题目录内；越界或指到课题根目录本身都返回 None。

    课题根目录就是该课题全部资料，_is_within_root 认为 '.' 属于根内，
    删除/重命名必须额外排除根目录，否则一次请求就能清空整个课题。
    """
    if relative_path is None or str(relative_path).strip() == '':
        return None
    target = os.path.normpath(_build_abs_path(root, relative_path))
    if not _is_within_root(target, root):
        return None
    if os.path.normcase(os.path.normpath(target)) == os.path.normcase(os.path.normpath(str(root))):
        return None
    return target


PROTECTED_FOLDER_RE = re.compile(r'^0[1-6]')

def _is_protected_folder(abs_path):
    """01~06 是 PRD 规定的标准目录，界面不给删除入口，后端也必须拒绝。

    只在模板里藏住链接挡不住直接 POST /file/delete/：实测一次请求就能删掉
    01_申报，整夹申报资料随之消失，所以校验要落在服务端而不是只靠前端。
    """
    return bool(PROTECTED_FOLDER_RE.match(os.path.basename(os.path.normpath(str(abs_path)))))


def _unique_upload_path(target_dir, filename, max_suffix=999):
    """同名文件不覆盖，自动改成 名称_1.ext；上传不应静默丢资料。"""
    candidate = os.path.join(target_dir, filename)
    if not os.path.exists(candidate):
        return candidate

    stem, ext = os.path.splitext(filename)
    for index in range(1, max_suffix + 1):
        candidate = os.path.join(target_dir, f"{stem}_{index}{ext}")
        if not os.path.exists(candidate):
            return candidate
    return os.path.join(target_dir, f"{stem}_{int(time.time())}{ext}")

def _get_progress_end_date(project):
    """进度监控使用延期日期；未填写延期日期时使用计划结题日期。"""
    return project.extension_date or project.planned_end_date


def _compute_progress_node(project, today):
    completed_statuses = {'结题', '终止'}
    completed = project.status in completed_statuses or bool(project.actual_completion_date)

    start_date = project.start_date
    end_date = _get_progress_end_date(project)
    progress_available = bool(start_date and end_date)

    midpoint_date = None
    total_days = None
    elapsed_days = None
    progress_percent = 0
    days_remaining = None
    overdue_days = None
    midterm_reached = False
    overdue = False

    if progress_available:
        total_days = max((end_date - start_date).days, 1)
        elapsed_days = (today - start_date).days
        progress_ratio = elapsed_days / total_days
        progress_ratio = min(max(progress_ratio, 0), 1)
        progress_percent = int(round(progress_ratio * 100))
        midpoint_date = start_date + timedelta(days=total_days // 2)
        midterm_reached = today >= midpoint_date and today >= start_date
        days_remaining = (end_date - today).days
        overdue = today > end_date and not completed
        if overdue:
            overdue_days = (today - end_date).days

    if completed:
        node_status = '已完成'
        status_key = 'complete'
    elif not progress_available:
        node_status = '缺少日期'
        status_key = 'unknown'
    elif today < start_date:
        node_status = '未开始'
        status_key = 'pending'
    elif overdue:
        node_status = '超期'
        status_key = 'overdue'
    elif midterm_reached:
        node_status = '已到中期'
        status_key = 'midterm'
    else:
        node_status = '未到中期'
        status_key = 'ontrack'

    return {
        'start_date': start_date,
        'end_date': end_date,
        'midpoint_date': midpoint_date,
        'progress_available': progress_available,
        'progress_percent': progress_percent,
        'total_days': total_days,
        'elapsed_days': elapsed_days,
        'days_remaining': days_remaining,
        'overdue_days': overdue_days,
        'midterm_reached': midterm_reached,
        'overdue': overdue,
        'node_status': node_status,
        'status_key': status_key,
        'midpoint_percent': 50 if progress_available else None,
    }


def _clean_match_text(value):
    return clean_match_text(value)


def _similarity_score(left, right):
    return similarity_score(left, right)


def _parse_threshold(value, default=0.85):
    try:
        threshold = float(value)
    except (TypeError, ValueError):
        threshold = default
    if threshold < 0.5:
        threshold = 0.5
    if threshold > 0.98:
        threshold = 0.98
    return threshold


def _split_query_terms(query):
    if not query:
        return []
    terms = re.split(r'[,，;；\s]+', str(query).strip())
    return [term for term in terms if term]


def _parse_decimal_param(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r'-?\d+(?:\.\d+)?', text)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except Exception:
        return None


def _extract_list_param(request, key):
    values = []
    for item in request.GET.getlist(key):
        if item is None:
            continue
        text = str(item).strip()
        if text:
            values.append(text)
    return values


def _apply_multi_value_filter(queryset, field_name, values):
    if not values:
        return queryset
    include_blank = '__blank__' in values
    clean_values = [v for v in values if v != '__blank__']
    if include_blank and clean_values:
        return queryset.filter(
            Q(**{f'{field_name}__in': clean_values}) |
            Q(**{f'{field_name}__isnull': True}) |
            Q(**{f'{field_name}__exact': ''})
        )
    if include_blank:
        return queryset.filter(
            Q(**{f'{field_name}__isnull': True}) |
            Q(**{f'{field_name}__exact': ''})
        )
    return queryset.filter(**{f'{field_name}__in': clean_values})


def _apply_project_filters(queryset, request):
    query = request.GET.get('q', '').strip()
    terms = _split_query_terms(query)
    for term in terms:
        queryset = queryset.filter(
            Q(name__icontains=term) |
            Q(project_id__icontains=term) |
            Q(project_lead__icontains=term) |
            Q(contact_person__icontains=term) |
            Q(ownership__icontains=term) |
            Q(managing_unit__icontains=term) |
            Q(level__icontains=term) |
            Q(project_type__icontains=term) |
            Q(role__icontains=term) |
            Q(status__icontains=term) |
            Q(research_content__icontains=term) |
            Q(remarks__icontains=term)
        )

    year_values = _extract_list_param(request, 'year')
    if year_values:
        year_numbers = []
        for value in year_values:
            try:
                year_numbers.append(int(value))
            except (TypeError, ValueError):
                continue
        if year_numbers:
            queryset = queryset.filter(start_year__in=year_numbers)

    status_values = _extract_list_param(request, 'status')
    queryset = _apply_multi_value_filter(queryset, 'status', status_values)

    level_values = _extract_list_param(request, 'level')
    queryset = _apply_multi_value_filter(queryset, 'level', level_values)

    ownership_values = _extract_list_param(request, 'ownership')
    queryset = _apply_multi_value_filter(queryset, 'ownership', ownership_values)

    type_values = _extract_list_param(request, 'project_type')
    queryset = _apply_multi_value_filter(queryset, 'project_type', type_values)

    role_values = _extract_list_param(request, 'role')
    queryset = _apply_multi_value_filter(queryset, 'role', role_values)

    unit_values = _extract_list_param(request, 'managing_unit')
    queryset = _apply_multi_value_filter(queryset, 'managing_unit', unit_values)

    lead_values = _extract_list_param(request, 'project_lead')
    queryset = _apply_multi_value_filter(queryset, 'project_lead', lead_values)

    min_budget = _parse_decimal_param(request.GET.get('min_budget'))
    if min_budget is not None:
        queryset = queryset.filter(total_budget__gte=min_budget)

    max_budget = _parse_decimal_param(request.GET.get('max_budget'))
    if max_budget is not None:
        queryset = queryset.filter(total_budget__lte=max_budget)

    start_date_from = parse_date(request.GET.get('start_date_from', '').strip())
    if start_date_from:
        queryset = queryset.filter(start_date__gte=start_date_from)

    start_date_to = parse_date(request.GET.get('start_date_to', '').strip())
    if start_date_to:
        queryset = queryset.filter(start_date__lte=start_date_to)

    end_date_from = parse_date(request.GET.get('end_date_from', '').strip())
    if end_date_from:
        queryset = queryset.filter(planned_end_date__gte=end_date_from)

    end_date_to = parse_date(request.GET.get('end_date_to', '').strip())
    if end_date_to:
        queryset = queryset.filter(planned_end_date__lte=end_date_to)

    return queryset


def _valid_funding_category(value):
    value = str(value or '').strip()
    valid = {key for key, _label in Project.FUNDING_CATEGORY_CHOICES}
    return value if value in valid else 'special'


def _funding_context(category):
    labels = dict(Project.FUNDING_CATEGORY_CHOICES)
    return {
        'funding_category': category,
        'funding_category_label': labels.get(category, labels['special']),
        'is_self_funded': category == 'self_funded',
        'scope_url': reverse('self_funded_project_list') if category == 'self_funded' else reverse('project_list'),
    }


def _get_completion_date(project):
    return project.actual_completion_date or project.extension_date or project.planned_end_date


def _move_path_with_backup(src_path, dest_path):
    if not src_path or not os.path.exists(src_path):
        return False, None
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    backup_path = None
    if os.path.exists(dest_path):
        backup_path = dest_path + '_backup_' + str(int(time.time()))
        shutil.move(dest_path, backup_path)
    shutil.move(src_path, dest_path)
    return True, backup_path

def _sanitize_folder_segment(value, fallback='未命名'):
    text = str(value or '').strip()
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', text)
    text = re.sub(r'\s+', ' ', text).strip(' .')
    if not text:
        return fallback
    reserved_names = {
        'CON', 'PRN', 'AUX', 'NUL',
        *(f'COM{i}' for i in range(1, 10)),
        *(f'LPT{i}' for i in range(1, 10)),
    }
    if text.upper() in reserved_names:
        text = f'_{text}'
    return text

def _get_project_folder_name(project):
    start_year = _sanitize_folder_segment(project.start_year, '未知年份')
    status = _sanitize_folder_segment(project.status, '未设置状态')
    project_id = _sanitize_folder_segment(project.project_id, '未编号')
    name = _sanitize_folder_segment(project.name, '未命名课题')
    return f"{start_year}-{status}-{project_id}-{name}"

def _find_adoptable_legacy_dir(project, expected_folder_name):
    """目标目录缺失时按编号找旧目录；命中多个或疑似他人目录一律放弃，交人工确认。

    课题编号可能是另一个编号的前缀（如 CSCEC-2017-Z-4 与 CSCEC-2017-Z-48），
    子串匹配会把别人的课题目录认领过来，因此只在候选唯一、且不属于其他课题时才敢动手。
    """
    projects_root = str(settings.PROJECTS_ROOT)
    if not os.path.isdir(projects_root):
        return None

    owned = set()
    for other in Project.objects.exclude(pk=project.pk).only('project_id', 'start_year', 'status', 'name', 'directory_path'):
        owned.add(_normalize_path(os.path.join(
            projects_root, _get_project_folder_name(other))))
        if other.directory_path:
            owned.add(_normalize_path(other.directory_path))

    candidates = []
    for item in os.listdir(projects_root):
        if item == expected_folder_name or project.project_id not in item:
            continue
        item_path = os.path.join(projects_root, item)
        if not os.path.isdir(item_path):
            continue
        if _normalize_path(item_path) in owned:
            return None
        candidates.append(item_path)
    return candidates[0] if len(candidates) == 1 else None


def create_project_directory_structure(project):
    """根据PRD文档4.3节要求创建课题目录结构"""
    folder_name = _get_project_folder_name(project)
    
    # 确保projects根目录存在
    projects_root = str(settings.PROJECTS_ROOT)
    os.makedirs(projects_root, exist_ok=True)
    
    base_dir = os.path.join(projects_root, folder_name)
    
    # 按照文档4.3节规定的目录结构
    required_dirs = [
        '01_申报',
        '02_立项', 
        '03_开题及任务书',
        '04_中期',
        '05_变更',
        '06_结题',
        '07_其它',
    ]
    
    try:
        # 目录尚未落地时，先尝试认领唯一可辨认的旧目录，避免新建空目录把资料孤立掉
        if not os.path.isdir(base_dir):
            legacy_dir = _find_adoptable_legacy_dir(project, folder_name)
            if legacy_dir:
                try:
                    os.rename(legacy_dir, base_dir)
                except OSError:
                    pass

        # 确保项目基础目录存在
        os.makedirs(base_dir, exist_ok=True)

        # 兼容旧目录名称：03_开题 -> 03_开题及任务书
        legacy_dir = os.path.join(base_dir, '03_开题')
        new_dir = os.path.join(base_dir, '03_开题及任务书')
        if os.path.isdir(legacy_dir) and not os.path.exists(new_dir):
            try:
                os.rename(legacy_dir, new_dir)
            except Exception as e:
                print(f"重命名子目录失败: {e}")
        
        # 创建所有必需的子目录
        for d in required_dirs:
            dir_path = os.path.join(base_dir, d)
            os.makedirs(dir_path, exist_ok=True)
        
        # 更新project的directory_path
        if project.directory_path != base_dir:
            project.directory_path = base_dir
            project.save(update_fields=['directory_path'])

    except Exception as e:
        logger.exception('创建项目目录失败: %s', base_dir)
        # 如果创建失败，至少设置一个基本路径
        if not project.directory_path:
            project.directory_path = base_dir
            project.save(update_fields=['directory_path'])

def rename_project_folder(request, project, old_path):
    """重命名项目文件夹"""
    import time
    new_folder_name = _get_project_folder_name(project)
    projects_root = str(settings.PROJECTS_ROOT)
    new_path = os.path.join(projects_root, new_folder_name)
    
    if old_path != new_path:
        try:
            if os.path.exists(old_path):
                # 如果新路径已存在，先删除或重命名
                if os.path.exists(new_path):
                    backup_path = new_path + '_backup_' + str(int(time.time()))
                    shutil.move(new_path, backup_path)
                    messages.warning(request, f"原目录已备份为: {os.path.basename(backup_path)}")
                
                shutil.move(old_path, new_path)
                messages.success(request, f"项目目录已重命名为: {new_folder_name}")
            
            # 无论是否移动成功，都更新数据库中的路径
            project.directory_path = new_path
            project.save(update_fields=['directory_path'])
            
        except Exception as e:
            messages.error(request, f"重命名项目目录失败: {e}")
            # 即使重命名失败，也要更新数据库路径以保持一致性
            project.directory_path = new_path
            project.save(update_fields=['directory_path'])
    elif not os.path.exists(new_path):
        create_project_directory_structure(project)

def get_directory_level(path, base_path=None):
    """读取单层目录，文件夹内容在用户展开时再按需获取。"""
    import datetime

    def format_file_size(size_bytes):
        if size_bytes == 0:
            return "0 B"
        size_names = ["B", "KB", "MB", "GB"]
        index = 0
        value = float(size_bytes)
        while value >= 1024 and index < len(size_names) - 1:
            value /= 1024.0
            index += 1
        return f"{value:.1f} {size_names[index]}"

    if base_path is None:
        base_path = path
    if not os.path.isdir(path):
        return []

    try:
        with os.scandir(path) as scan_entries:
            entries = sorted(scan_entries, key=lambda entry: entry.name.lower())
    except OSError:
        return []

    nodes = []
    for entry in entries:
        item_path = entry.path
        rel_path = _relpath_for_tree(item_path, base_path)
        try:
            is_directory = entry.is_dir(follow_symlinks=False)
        except OSError:
            is_directory = False

        node = {
            'name': entry.name,
            'path': rel_path,
            # 模板里拼 ?path= 查询串时直接用，避免文件名含 # & 空格时链接被截断
            'path_qs': quote(rel_path),
            'type': 'folder' if is_directory else 'file',
            'children': [],
        }

        if is_directory:
            has_children = False
            file_count = 0
            folder_count = 0
            try:
                with os.scandir(item_path) as child_entries:
                    for child in child_entries:
                        has_children = True
                        try:
                            if child.is_dir(follow_symlinks=False):
                                folder_count += 1
                            else:
                                file_count += 1
                        except OSError:
                            file_count += 1
            except OSError:
                pass
            node.update({
                'has_children': has_children,
                'file_count': file_count,
                'folder_count': folder_count,
                'total_items': file_count + folder_count,
            })
        else:
            try:
                stat_result = entry.stat(follow_symlinks=False)
                node['size'] = format_file_size(stat_result.st_size)
                node['size_bytes'] = stat_result.st_size
                node['modified_time'] = datetime.datetime.fromtimestamp(
                    stat_result.st_mtime
                ).strftime('%Y-%m-%d %H:%M')
            except OSError:
                node['size'] = "未知"
                node['size_bytes'] = 0
                node['modified_time'] = "未知"
            node['has_children'] = False

        nodes.append(node)
    return nodes

def create_project_view(request):
    category = _valid_funding_category(request.POST.get('funding_category') or request.GET.get('funding_category'))
    if request.method == 'POST':
        form = ProjectForm(request.POST)
        if form.is_valid():
            project = form.save()
            create_project_directory_structure(project)
            return redirect('self_funded_project_list' if project.funding_category == 'self_funded' else 'project_list')
    else:
        form = ProjectForm(initial={'funding_category': category})
    return render(request, 'core/create_project.html', {'form': form, **_funding_context(category)})

def _normalize_text(value):
    if value is None:
        return ''
    return str(value).strip()

def _pick_first(*values):
    for value in values:
        text = _normalize_text(value)
        if text:
            return text
    return ''

def _safe_date(year, month, day):
    try:
        return datetime(int(year), int(month), int(day)).date()
    except ValueError:
        try:
            return datetime(int(year), int(month), 1).date()
        except ValueError:
            return None

def _find_dates(text):
    if not text:
        return []
    results = []
    patterns = [
        r'(\d{4})[./-](\d{1,2})[./-](\d{1,2})',
        r'(\d{4})年(\d{1,2})月(\d{1,2})日?',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            date_obj = _safe_date(match.group(1), match.group(2), match.group(3))
            if date_obj and date_obj not in results:
                results.append(date_obj)
    for match in re.finditer(r'(\d{4})年(\d{1,2})月', text):
        date_obj = _safe_date(match.group(1), match.group(2), 1)
        if date_obj and date_obj not in results:
            results.append(date_obj)
    return results

def _extract_date_range(text):
    dates = _find_dates(text)
    if not dates:
        return None, None
    if len(dates) == 1:
        return dates[0], None
    return dates[0], dates[-1]

def _coerce_number(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _normalize_text(value)
    if not text:
        return None
    match = re.search(r'-?\d+(?:\.\d+)?', text)
    if match:
        try:
            return float(match.group(0))
        except ValueError:
            return None
    return None

def _format_number(value):
    if value is None:
        return ''
    if isinstance(value, (int,)):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip('0').rstrip('.')

def _map_ownership(text):
    if not text:
        return ''
    if '地下空间' in text:
        return '地下空间'
    if '西勘院' in text:
        return '西勘院'
    return ''

def _is_placeholder_name(text):
    if not text:
        return True
    compact = re.sub(r'\s+', '', text)
    return compact in {'姓名', '姓名:', '姓', '姓名/'} or compact == '姓名'

def _extract_budget_totals(rows):
    if not rows:
        return None
    for keyword in ('经费支出（合计）', '经费来源（合计）'):
        for row in rows:
            if keyword in _normalize_text(row.get('预算科目名称')):
                return row
    return rows[0]

def _parse_funding_text(text):
    if not text:
        return {}
    result = {}
    text = str(text)
    patterns = [
        (r'(?:\u9662\u4e13\u9879)[:\uff1a]?\s*(\d+(?:\.\d+)?)', 'institute_funding'),
        (r'(?<!\u9662)\u4e13\u9879[:\uff1a]?\s*(\d+(?:\.\d+)?)', 'external_funding'),
        (r'\u81ea\u7b79[:\uff1a]?\s*(\d+(?:\.\d+)?)', 'unit_funding'),
        (r'(?:\u5408\u8ba1|\u603b\u8ba1)[:\uff1a]?\s*(\d+(?:\.\d+)?)', 'total_budget'),
    ]
    for pattern, key in patterns:
        match = re.search(pattern, text)
        if match and key not in result:
            try:
                result[key] = float(match.group(1))
            except ValueError:
                continue
    return result


def _map_docx_to_project_fields(extracted):
    basic = extracted.get('basic_info', {}) or {}
    fields = extracted.get('topic_info', {}).get('fields', {}) or {}
    budget_rows = extracted.get('budget_summary', {}).get('rows', []) or []

    project_id = _pick_first(basic.get('\u8bfe\u9898\u7f16\u53f7'), fields.get('\u8bfe\u9898\u7f16\u53f7'))
    name = _pick_first(basic.get('\u8bfe\u9898\u540d\u79f0'), fields.get('\u8bfe\u9898\u540d\u79f0'))
    managing_unit = _pick_first(
        basic.get('\u8bfe\u9898\u627f\u62c5\u5355\u4f4d'),
        basic.get('\u8bfe\u9898\u7275\u5934\u627f\u62c5\u5355\u4f4d'),
        fields.get('\u8bfe\u9898\u7ec4\u7ec7\u5355\u4f4d')
    )
    ownership = _map_ownership(managing_unit)

    lead_name = _pick_first(
        basic.get('\u8bfe\u9898\u8d1f\u8d23\u4eba'),
        fields.get('\u59d3\u540d'),
        fields.get('\u8bfe\u9898\u8d1f\u8d23\u4eba')
    )
    if _is_placeholder_name(lead_name):
        lead_name = ''

    contact_person = _pick_first(
        basic.get('\u8bfe\u9898\u8054\u7cfb\u4eba'),
        fields.get('\u8bfe\u9898\u8054\u7cfb\u4eba')
    )

    start_date_text = _pick_first(fields.get('\u8d77\u59cb\u65f6\u95f4'))
    end_date_text = _pick_first(fields.get('\u7ec8\u6b62\u65f6\u95f4'))
    start_date = None
    end_date = None

    if start_date_text:
        dates = _find_dates(start_date_text)
        start_date = dates[0] if dates else None
    if end_date_text:
        dates = _find_dates(end_date_text)
        end_date = dates[0] if dates else None
    if not start_date and not end_date:
        date_range_text = _pick_first(basic.get('\u8bfe\u9898\u8d77\u6b62\u5e74\u9650'))
        start_date, end_date = _extract_date_range(date_range_text)

    budget_row = _extract_budget_totals(budget_rows)
    total_budget = _coerce_number(budget_row.get('\u5408\u8ba1')) if budget_row else None
    external_funding = _coerce_number(budget_row.get('\u4e13\u9879\u7ecf\u8d39')) if budget_row else None
    institute_funding = _coerce_number(budget_row.get('\u9662\u4e13\u9879\u7ecf\u8d39')) if budget_row else None
    unit_funding = _coerce_number(budget_row.get('\u81ea\u7b79\u7ecf\u8d39')) if budget_row else None
    if unit_funding is None and budget_row:
        unit_funding = _coerce_number(budget_row.get('\u6240\u5c5e\u5355\u4f4d\u81ea\u7b79\u8d44\u91d1'))

    funding_text = _pick_first(basic.get('\u7acb\u9879\u7ecf\u8d39'), fields.get('\u7ecf\u8d39\u9884\u7b97'))
    funding_values = _parse_funding_text(funding_text)
    if total_budget is None:
        total_budget = funding_values.get('total_budget')
    if institute_funding is None:
        institute_funding = funding_values.get('institute_funding')
    if external_funding is None:
        external_funding = funding_values.get('external_funding')
    if unit_funding is None:
        unit_funding = funding_values.get('unit_funding')
    if total_budget is None:
        parts = [v for v in (external_funding, institute_funding, unit_funding) if v is not None]
        if parts:
            total_budget = sum(parts)

    research_content = _normalize_text(fields.get('\u4e3b\u8981\u7814\u7a76\u5185\u5bb9'))

    mapped = {}
    if project_id:
        mapped['project_id'] = project_id
    if name:
        mapped['name'] = name
    if managing_unit:
        mapped['managing_unit'] = managing_unit
    if ownership:
        mapped['ownership'] = ownership
    if lead_name:
        mapped['project_lead'] = lead_name
    if contact_person:
        mapped['contact_person'] = contact_person
    if start_date:
        mapped['start_date'] = start_date.isoformat()
        mapped['start_year'] = str(start_date.year)
    if end_date:
        mapped['planned_end_date'] = end_date.isoformat()
    if total_budget is not None:
        mapped['total_budget'] = _format_number(total_budget)
    if external_funding is not None:
        mapped['external_funding'] = _format_number(external_funding)
    if institute_funding is not None:
        mapped['institute_funding'] = _format_number(institute_funding)
    if unit_funding is not None:
        mapped['unit_funding'] = _format_number(unit_funding)
    if research_content:
        mapped['research_content_manual'] = research_content
    return mapped

def _convert_doc_to_docx(doc_path):
    temp_dir = tempfile.mkdtemp()
    soffice_candidates = [
        shutil.which('soffice'),
        shutil.which('soffice.exe'),
        r'C:\Program Files\LibreOffice\program\soffice.exe',
        r'C:\Program Files (x86)\LibreOffice\program\soffice.exe',
    ]
    soffice_path = next((p for p in soffice_candidates if p and os.path.exists(p)), None)
    if not soffice_path:
        raise RuntimeError('未检测到LibreOffice，请安装后再解析.doc文件。')
    result = subprocess.run(
        [soffice_path, '--headless', '--convert-to', 'docx', '--outdir', temp_dir, doc_path],
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='ignore'
    )
    dest_path = os.path.join(temp_dir, f"{Path(doc_path).stem}.docx")
    if result.returncode != 0 or not os.path.exists(dest_path):
        candidates = list(Path(temp_dir).glob('*.docx'))
        if candidates:
            dest_path = str(candidates[0])
        else:
            error = (result.stderr or '').strip() or (result.stdout or '').strip() or 'DOC转DOCX失败'
            raise RuntimeError(error)
    return dest_path, temp_dir


@require_POST
def extract_task_docx_view(request):
    uploaded_file = request.FILES.get('document')
    if not uploaded_file:
        return JsonResponse({'success': False, 'message': '请选择要解析的任务书文件。'}, status=400)

    ext = os.path.splitext(uploaded_file.name)[1].lower()
    if ext not in ['.docx', '.doc', '.pdf']:
        return JsonResponse({'success': False, 'message': '仅支持 .doc、.docx 或 .pdf 格式的任务书。'}, status=400)

    temp_path = None
    converted_path = None
    temp_dir = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            for chunk in uploaded_file.chunks():
                tmp.write(chunk)
            temp_path = tmp.name

        parse_path = temp_path
        if ext == '.doc':
            converted_path, temp_dir = _convert_doc_to_docx(temp_path)
            parse_path = converted_path

        extracted = extract_task_pdf(parse_path) if ext == '.pdf' else extract_task_docx(parse_path)
        mapped = _map_docx_to_project_fields(extracted)
        return JsonResponse({'success': True, 'data': mapped})
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'解析失败：{e}'}, status=422)
    finally:
        for path_to_remove in (temp_path, converted_path):
            if path_to_remove and os.path.exists(path_to_remove):
                try:
                    os.remove(path_to_remove)
                except OSError:
                    pass
        if temp_dir and os.path.isdir(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except OSError:
                pass

def project_list_view(request, funding_category=None):
    category = _valid_funding_category(funding_category or request.GET.get('funding_category'))
    scoped_projects = Project.objects.filter(funding_category=category)
    queryset = _apply_project_filters(scoped_projects, request)
    allowed_sorts = {
        'name': 'name',
        '-name': '-name',
        'start_year': 'start_year',
        '-start_year': '-start_year',
        'status': 'status',
        '-status': '-status',
        'total_budget': 'total_budget',
        '-total_budget': '-total_budget',
        'external_funding': 'external_funding',
        '-external_funding': '-external_funding',
    }
    sort = request.GET.get('sort', '-start_year')
    if sort not in allowed_sorts:
        sort = '-start_year'
    queryset = queryset.order_by(allowed_sorts[sort], 'project_id')
    distinct_years = scoped_projects.values_list('start_year', flat=True).distinct().order_by('-start_year')
    distinct_statuses = scoped_projects.values_list('status', flat=True).distinct().order_by('status')
    distinct_levels = scoped_projects.values_list('level', flat=True).distinct().order_by('level')
    distinct_ownerships = scoped_projects.values_list('ownership', flat=True).distinct().order_by('ownership')
    distinct_types = scoped_projects.values_list('project_type', flat=True).distinct().order_by('project_type')
    distinct_roles = scoped_projects.values_list('role', flat=True).distinct().order_by('role')
    distinct_units = scoped_projects.exclude(managing_unit__isnull=True).exclude(managing_unit__exact='').values_list('managing_unit', flat=True).distinct().order_by('managing_unit')
    distinct_leads = scoped_projects.exclude(project_lead__isnull=True).exclude(project_lead__exact='').values_list('project_lead', flat=True).distinct().order_by('project_lead')

    selected_years = _extract_list_param(request, 'year')
    selected_statuses = _extract_list_param(request, 'status')
    selected_levels = _extract_list_param(request, 'level')
    selected_ownerships = _extract_list_param(request, 'ownership')
    selected_types = _extract_list_param(request, 'project_type')
    selected_roles = _extract_list_param(request, 'role')
    selected_units = _extract_list_param(request, 'managing_unit')
    selected_leads = _extract_list_param(request, 'project_lead')
    min_budget = request.GET.get('min_budget', '').strip()
    max_budget = request.GET.get('max_budget', '').strip()
    start_date_from = request.GET.get('start_date_from', '').strip()
    start_date_to = request.GET.get('start_date_to', '').strip()
    end_date_from = request.GET.get('end_date_from', '').strip()
    end_date_to = request.GET.get('end_date_to', '').strip()
    query = request.GET.get('q', '').strip()
    filters_applied = any([
        query, selected_years, selected_statuses, selected_levels, selected_ownerships,
        selected_types, selected_roles, selected_units, selected_leads,
        min_budget, max_budget, start_date_from, start_date_to, end_date_from, end_date_to
    ])
    active_filter_count = sum(bool(value) for value in [
        query, selected_years, selected_statuses, selected_levels, selected_ownerships,
        selected_types, selected_roles, selected_units, selected_leads,
        min_budget, max_budget, start_date_from, start_date_to, end_date_from, end_date_to,
    ])

    def build_sort_link(field_name):
        params = request.GET.copy()
        params.pop('page', None)
        params['sort'] = f'-{field_name}' if sort == field_name else field_name
        if sort == field_name:
            icon = 'fas fa-sort-up'
            label = '当前升序，点击改为降序'
        elif sort == f'-{field_name}':
            icon = 'fas fa-sort-down'
            label = '当前降序，点击改为升序'
        else:
            icon = 'fas fa-sort'
            label = '点击排序'
        return {'url': f'?{params.urlencode()}', 'icon': icon, 'label': label}

    sort_links = {
        field_name: build_sort_link(field_name)
        for field_name in ('name', 'start_year', 'status', 'external_funding')
    }

    # Summary stats
    from django.db.models import Sum
    total_projects = queryset.count()
    completed_projects = queryset.filter(status__in=['结题', '终止']).count()
    ongoing_projects = total_projects - completed_projects
    total_budget = queryset.aggregate(Sum('total_budget'))['total_budget__sum'] or 0
    paginator = Paginator(queryset, 30)
    page_obj = paginator.get_page(request.GET.get('page'))
    pagination_params = request.GET.copy()
    pagination_params.pop('page', None)

    context = {
        'projects': page_obj.object_list,
        'page_obj': page_obj,
        'pagination_query': pagination_params.urlencode(),
        'distinct_years': distinct_years,
        'distinct_statuses': distinct_statuses,
        'distinct_levels': distinct_levels,
        'distinct_ownerships': distinct_ownerships,
        'distinct_types': distinct_types,
        'distinct_roles': distinct_roles,
        'distinct_units': distinct_units,
        'distinct_leads': distinct_leads,
        'total_projects': total_projects,
        'ongoing_projects': ongoing_projects,
        'completed_projects': completed_projects,
        'total_budget': total_budget,
        'selected_years': selected_years,
        'selected_statuses': selected_statuses,
        'selected_levels': selected_levels,
        'selected_ownerships': selected_ownerships,
        'selected_types': selected_types,
        'selected_roles': selected_roles,
        'selected_units': selected_units,
        'selected_leads': selected_leads,
        'min_budget': min_budget,
        'max_budget': max_budget,
        'start_date_from': start_date_from,
        'start_date_to': start_date_to,
        'end_date_from': end_date_from,
        'end_date_to': end_date_to,
        'query': query,
        'filters_applied': filters_applied,
        'active_filter_count': active_filter_count,
        'sort': sort,
        'sort_links': sort_links,
        **_funding_context(category),
    }
    return render(request, 'core/project_list.html', context)


BUDGET_FIELDS = ('total_budget', 'external_funding', 'institute_funding', 'unit_funding')

# 导出/导入共用的经费列名以 Project.verbose_name 为准；这里只保留历史总表用过的写法。
LEGACY_BUDGET_HEADERS = {
    '总预算': 'total_budget',
    '预算总额': 'total_budget',
    '经费合计': 'total_budget',
    '外部专项经费': 'external_funding',
    '外部专项资金': 'external_funding',
    '专项经费': 'external_funding',
    '院专项经费': 'institute_funding',
    '院自筹': 'institute_funding',
    '院自筹经费': 'institute_funding',
    '所属单位自筹经费': 'unit_funding',
    '所属单位自筹资金': 'unit_funding',
    '单位自筹经费': 'unit_funding',
    '自筹经费': 'unit_funding',
}


def normalize_excel_header(value):
    """去掉表头里的空格与“（万元）”一类单位标注，用于匹配列名。"""
    text = force_str(value if value is not None else '').strip()
    return re.sub(r'[（(]\s*(?:单位[：:]\s*)?万元\s*[)）]|[^\w一-鿿]+', '', text)


def budget_header_map():
    header_map = {}
    for field_name in BUDGET_FIELDS:
        verbose_name = Project._meta.get_field(field_name).verbose_name
        header_map[normalize_excel_header(verbose_name)] = field_name
    for header, field_name in LEGACY_BUDGET_HEADERS.items():
        header_map.setdefault(normalize_excel_header(header), field_name)
    return header_map


def export_project_list_view(request, funding_category=None):
    category = _valid_funding_category(funding_category or request.GET.get('funding_category'))
    queryset = _apply_project_filters(Project.objects.filter(funding_category=category), request)

    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = '课题清单（单位：万元）'

    headers = [
        '课题编号', '课题名称', '课题归属', '归口单位', '课题级别', '课题类型', '参与角色',
        '开始年份', '课题状态', '课题联系人', '课题负责人', '开始日期', '计划结束日期',
        '延期时间', '实际结题时间',
        # 经费四列直接取模型 verbose_name，避免导出与系统字段名再次分叉。
        *[Project._meta.get_field(field_name).verbose_name for field_name in BUDGET_FIELDS],
        '经费管理类别', '主要研究内容', '备注',
    ]
    worksheet.append(headers)

    for cell in worksheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal='center', vertical='center')

    for project in queryset:
        worksheet.append([
            project.project_id,
            project.name,
            project.ownership,
            project.managing_unit,
            project.level,
            project.project_type,
            project.role,
            project.start_year,
            project.status,
            project.contact_person,
            project.project_lead,
            project.start_date.isoformat() if project.start_date else '',
            project.planned_end_date.isoformat() if project.planned_end_date else '',
            project.extension_date.isoformat() if project.extension_date else '',
            project.actual_completion_date.isoformat() if project.actual_completion_date else '',
            float(project.total_budget) if project.total_budget is not None else '',
            float(project.external_funding) if project.external_funding is not None else '',
            float(project.institute_funding) if project.institute_funding is not None else '',
            float(project.unit_funding) if project.unit_funding is not None else '',
            project.get_funding_category_display(),
            project.research_content,
            project.remarks,
        ])

    worksheet.freeze_panes = 'A2'
    worksheet.auto_filter.ref = worksheet.dimensions

    for column_cells in worksheet.columns:
        column_letter = get_column_letter(column_cells[0].column)
        max_length = max(len(str(cell.value or '')) for cell in column_cells)
        worksheet.column_dimensions[column_letter].width = min(max(max_length + 2, 12), 40)
        if column_letter in {'T', 'U'}:
            for cell in column_cells[1:]:
                cell.alignment = Alignment(vertical='top', wrap_text=True)

    output = BytesIO()
    workbook.save(output)
    output.seek(0)

    timestamp = timezone.localtime().strftime('%Y%m%d_%H%M%S')
    filename = f'课题清单_{timestamp}.xlsx'

    response = HttpResponse(
        output.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f"attachment; filename*=UTF-8''{quote(filename)}"
    return response

def progress_monitor_view(request, funding_category=None):
    category = _valid_funding_category(funding_category or request.GET.get('funding_category'))
    scoped_projects = Project.objects.filter(funding_category=category)
    queryset = scoped_projects
    distinct_years = scoped_projects.values_list('start_year', flat=True).distinct().order_by('-start_year')
    distinct_statuses = scoped_projects.values_list('status', flat=True).distinct().order_by('status')
    progress_status_options = [
        {'value': 'ontrack', 'label': '\u672a\u5230\u4e2d\u671f'},
        {'value': 'midterm', 'label': '\u5df2\u5230\u4e2d\u671f'},
        {'value': 'overdue', 'label': '\u8d85\u671f'},
        {'value': 'pending', 'label': '\u672a\u5f00\u59cb'},
        {'value': 'unknown', 'label': '\u7f3a\u5c11\u65e5\u671f'},
        {'value': 'complete', 'label': '\u5df2\u5b8c\u6210'},
    ]
    valid_progress_status_values = {item['value'] for item in progress_status_options}

    query = request.GET.get('q')
    if query:
        queryset = queryset.filter(
            Q(name__icontains=query) |
            Q(project_id__icontains=query) |
            Q(project_lead__icontains=query) |
            Q(contact_person__icontains=query) |
            Q(ownership__icontains=query) |
            Q(managing_unit__icontains=query) |
            Q(level__icontains=query) |
            Q(project_type__icontains=query) |
            Q(research_content__icontains=query) |
            Q(remarks__icontains=query)
        )

    year = request.GET.get('year')
    if year:
        queryset = queryset.filter(start_year=year)

    status = request.GET.get('status')
    if status:
        queryset = queryset.filter(status=status)

    progress_status = (request.GET.get('progress_status') or '').strip()
    if progress_status not in valid_progress_status_values:
        progress_status = ''

    today = timezone.localdate()
    monitor_rows = []
    counts = {
        'total': 0,
        'ontrack': 0,
        'midterm': 0,
        'overdue': 0,
        'pending': 0,
        'unknown': 0,
        'complete': 0,
    }

    for project in queryset.order_by('-start_year', 'project_id'):
        node = _compute_progress_node(project, today)
        if progress_status and node['status_key'] != progress_status:
            continue
        monitor_rows.append({
            'project': project,
            **node,
        })
        counts['total'] += 1
        counts[node['status_key']] += 1

    # 先保留原有的“在研/延期优先”顺序，作为各优先级分组内的稳定顺序。
    monitor_rows.sort(
        key=lambda row: 0 if row['project'].status in {'在研', '延期'} else 1
    )

    def display_priority(row):
        if row['status_key'] == 'overdue':
            return 0
        if row['project'].status == '延期':
            return 1
        if row['status_key'] == 'midterm':
            return 2
        return 3

    # 总览主顺序：超期、延期、已到中期；其余沿用上面的原有顺序。
    monitor_rows.sort(key=display_priority)

    paginator = Paginator(monitor_rows, 30)
    page_obj = paginator.get_page(request.GET.get('page'))
    pagination_params = request.GET.copy()
    pagination_params.pop('page', None)

    context = {
        'monitor_rows': page_obj.object_list,
        'page_obj': page_obj,
        'pagination_query': pagination_params.urlencode(),
        'distinct_years': distinct_years,
        'distinct_statuses': distinct_statuses,
        'counts': counts,
        'today': today,
        'query': query or '',
        'selected_year': year or '',
        'selected_status': status or '',
        'progress_status_options': progress_status_options,
        'selected_progress_status': progress_status,
        **_funding_context(category),
    }
    return render(request, 'core/progress_monitor.html', context)


def _expense_data_file_path():
    configured = getattr(settings, 'EXPENSE_DATA_FILE', None)
    if configured:
        return Path(configured)
    return Path(settings.BASE_DIR) / 'zichouktfeiyong' / '支出监控_当前月.xlsx'


def expense_monitor_view(request, funding_category=None):
    category = _valid_funding_category(funding_category or request.GET.get('funding_category'))
    file_path = _expense_data_file_path()
    threshold = _parse_threshold(request.GET.get('threshold'), default=0.85)
    today = timezone.localdate()
    projects = list(Project.objects.order_by('project_id'))
    project_lookup = {project.project_id: project for project in projects}
    project_options = [(project.project_id, project.name) for project in projects]
    mapping_entries = list(ExpenseMapping.objects.select_related('project').all())

    analysis = {
        'sheet_name': '-',
        'source_row_count': 0,
        'filtered_rows': 0,
        'ignored_account_rows': 0,
        'description_group_total': 0,
        'project_rows': [],
        'matched_project_total': 0,
        'unmatched_rows': [],
        'negative_rows': [],
        'over_budget_alerts': [],
        'missing_budget_rows': [],
        'company_summaries': [
            {
                'company': abbreviate_company_name(company),
                'company_raw': company,
                'total': Decimal('0'),
                'matched_total': Decimal('0'),
                'unmatched_total': Decimal('0'),
                'row_count': 0,
            }
            for company in TARGET_COMPANIES
        ],
        'other_company_totals': {},
        'other_company_total': Decimal('0'),
        'total_expense_sum': Decimal('0'),
        'invalid_amount_total': 0,
    }
    file_exists = file_path.exists()
    file_mtime = None
    current_file_hash = ''
    imported = False
    analysis_ready = False

    if file_exists:
        try:
            file_mtime = datetime.fromtimestamp(
                file_path.stat().st_mtime,
                tz=timezone.get_current_timezone(),
            )
            current_file_hash = file_sha256(file_path)
            project_fingerprint = hashlib.sha256(repr((
                [
                    (
                        project.project_id,
                        project.name,
                        str(project.total_budget),
                        str(project.planned_end_date),
                        str(project.extension_date),
                        str(project.actual_completion_date),
                        project.funding_category,
                    )
                    for project in projects
                ],
                [
                    (mapping.normalized_text, mapping.project_id, str(mapping.updated_at))
                    for mapping in mapping_entries
                ],
            )).encode('utf-8')).hexdigest()[:20]
            analysis_cache_key = (
                f'expense-analysis-v7:{current_file_hash}:{threshold:.4f}:{project_fingerprint}'
            )
            cached_analysis = cache.get(analysis_cache_key)
            if cached_analysis is not None:
                analysis = cached_analysis
            else:
                analysis = analyze_expense_workbook(
                    file_path,
                    projects,
                    mappings=mapping_entries,
                    threshold=threshold,
                )
                cache.set(analysis_cache_key, analysis, 1800)
            analysis_ready = True
        except ExpenseWorkbookError as exc:
            messages.error(request, str(exc))
        except Exception as exc:
            messages.error(request, f'支出表分析失败：{exc}')
    else:
        messages.info(request, '尚未上传月度支出明细表。请上传包含6606数据的Excel文件。')

    latest_import = None
    comparison_base = None
    if analysis_ready:
        latest_import = ExpenseImport.objects.filter(
            format_version=EXPENSE_FORMAT_VERSION,
            file_sha256=current_file_hash,
        ).order_by('-created_at').first()
        if latest_import is None:
            original_filename = request.session.pop('expense_original_filename', '')
            with transaction.atomic():
                latest_import, imported = ExpenseImport.objects.get_or_create(
                    format_version=EXPENSE_FORMAT_VERSION,
                    file_sha256=current_file_hash,
                    defaults={
                        'source_file': str(file_path),
                        'sheet_name': analysis['sheet_name'],
                        'original_filename': original_filename or file_path.name,
                        'file_mtime': file_mtime,
                        'threshold': threshold,
                    },
                )
                if imported:
                    snapshots = []
                    for entry in analysis['project_rows']:
                        project = project_lookup.get(entry['project_id'])
                        if project is None:
                            continue
                        snapshots.append(ExpenseSnapshot(
                            import_log=latest_import,
                            project=project,
                            project_name=entry['project_name'],
                            company_name=entry['company_names'][:100],
                            matched_description=entry['sample_desc'] or '',
                            match_score=entry['max_score'],
                            total_expense=entry['total'],
                            funding_category=project.funding_category,
                        ))
                    if snapshots:
                        ExpenseSnapshot.objects.bulk_create(snapshots)
            if imported:
                messages.success(
                    request,
                    f'已生成新的月度快照：纳入 {analysis["filtered_rows"]} 条6606明细，'
                    f'归并匹配 {analysis["matched_project_total"]} 个系统课题。',
                )

        comparison_base = ExpenseImport.objects.filter(
            format_version=EXPENSE_FORMAT_VERSION,
        ).exclude(pk=latest_import.pk).order_by('-created_at').first()
    else:
        latest_import = ExpenseImport.objects.filter(
            format_version=EXPENSE_FORMAT_VERSION,
        ).order_by('-created_at').first()

    scoped_project_ids = {
        project.project_id for project in projects if project.funding_category == category
    }
    scoped_project_rows = [
        entry for entry in analysis['project_rows']
        if entry.get('project_id') in scoped_project_ids
    ]
    scoped_over_budget = [
        entry for entry in analysis['over_budget_alerts']
        if entry.get('project_id') in scoped_project_ids
    ]
    scoped_missing_budget = [
        entry for entry in analysis['missing_budget_rows']
        if entry.get('project_id') in scoped_project_ids
    ]
    scoped_company_totals = defaultdict(lambda: Decimal('0'))
    for entry in scoped_project_rows:
        for company in entry.get('company_breakdown', []):
            scoped_company_totals[company.get('raw_name') or company.get('company') or ''] += company.get('total', Decimal('0'))
    # 上传表是两家公司混合的，卡片继续展示全表6606口径（行数和金额来自整表分析），
    # 本类别只单独展示已匹配金额，避免把类别筛选后的数字误标成全表数字。
    scoped_company_summaries = []
    for summary in analysis['company_summaries']:
        scoped_company_summaries.append({
            **summary,
            'scoped_matched_total': scoped_company_totals.get(summary['company_raw'], Decimal('0')),
        })
    scoped_other_company_totals = {
        name: total for name, total in scoped_company_totals.items()
        if name and name not in TARGET_COMPANIES
    }
    display_analysis = dict(analysis)
    display_analysis.update({
        'project_rows': scoped_project_rows,
        'matched_project_total': len(scoped_project_rows),
        'over_budget_alerts': scoped_over_budget,
        'missing_budget_rows': scoped_missing_budget,
        'company_summaries': scoped_company_summaries,
        'other_company_totals': scoped_other_company_totals,
        'other_company_total': sum(scoped_other_company_totals.values(), Decimal('0')),
        # total_expense_sum 保持整表口径，卡片才不会被标成"6606累计支出合计"却只显示本类别；
        # 本类别自己的金额单独给 scoped_expense_total。
        'scoped_expense_total': sum(
            (entry.get('total', Decimal('0')) for entry in scoped_project_rows), Decimal('0')
        ),
        'global_matched_project_total': analysis['matched_project_total'],
    })

    growth_alerts = []
    comparison_time = comparison_base.created_at if comparison_base else None
    if analysis_ready and comparison_base:
        previous_totals = defaultdict(lambda: Decimal('0'))
        for snapshot in comparison_base.snapshots.all():
            if snapshot.project_id:
                previous_totals[snapshot.project_id] += snapshot.total_expense or Decimal('0')

        for entry in scoped_project_rows:
            completion_date = entry.get('completion_date')
            previous_total = previous_totals[entry['project_id']]
            if completion_date and completion_date <= today and entry['total'] > previous_total:
                growth_alerts.append({
                    'company_names': entry['company_names'],
                    'profit_centers': entry['profit_centers'],
                    'project_id': entry['project_id'],
                    'project_name': entry['project_name'],
                    'completion_date': completion_date,
                    'previous_total': previous_total,
                    'current_total': entry['total'],
                    'increase': entry['total'] - previous_total,
                })
        growth_alerts.sort(key=lambda item: item['increase'], reverse=True)

    # 系统里存在同名课题时程序无法判断钱属于哪一条，单独成区让人指定，
    # 混在未匹配列表里会被当成"匹配不上"而忽略掉。
    ambiguous_rows = [
        row for row in analysis['unmatched_rows'] if row['ambiguous_options']
    ]
    plain_unmatched_rows = [
        row for row in analysis['unmatched_rows'] if not row['ambiguous_options']
    ]
    unmatched_paginator = Paginator(plain_unmatched_rows, 25)
    unmatched_page_obj = unmatched_paginator.get_page(request.GET.get('unmatched_page'))
    unmatched_pagination_params = request.GET.copy()
    unmatched_pagination_params.pop('unmatched_page', None)
    other_company_rows = [
        {'company': company, 'total': total}
        for company, total in sorted(display_analysis['other_company_totals'].items())
    ]

    context = {
        **display_analysis,
        'unmatched_rows': unmatched_page_obj.object_list,
        'unmatched_page_obj': unmatched_page_obj,
        'unmatched_pagination_query': unmatched_pagination_params.urlencode(),
        'unmatched_total': len(plain_unmatched_rows),
        'ambiguous_rows': ambiguous_rows,
        'ambiguous_total': len(ambiguous_rows),
        'ambiguous_amount': sum((row['amount'] for row in ambiguous_rows), Decimal('0')),
        'unmatched_amount': sum((row['amount'] for row in analysis['unmatched_rows']), Decimal('0')),
        'negative_rows': analysis['negative_rows'][:200],
        'negative_total': len(analysis['negative_rows']),
        'total_rows': analysis['filtered_rows'],
        'file_path': str(file_path),
        'source_file_name': latest_import.original_filename if latest_import else file_path.name,
        'file_mtime': file_mtime,
        'threshold': threshold,
        'latest_import': latest_import,
        'comparison_time': comparison_time,
        'has_comparison': comparison_base is not None,
        'growth_alerts': growth_alerts,
        'imported': imported,
        'analysis_ready': analysis_ready,
        'expense_unit_label': EXPENSE_UNIT_LABEL,
        'over_budget_total': len(scoped_over_budget),
        'missing_budget_total': len(scoped_missing_budget),
        'project_options': project_options,
        'other_company_rows': other_company_rows,
        'account_label': '6606',
        'amount_column_label': LIFETIME_LABEL,
        **_funding_context(category),
        'expense_scope_note': '上传表保持混合；系统按已匹配课题的经费管理类别自动分流。未匹配与重名待指定的明细仍按整表口径展示。',
    }
    return render(request, 'core/expense_monitor.html', context)


@require_POST
def expense_import_view(request):
    category = _valid_funding_category(request.POST.get('funding_category'))
    redirect_name = 'self_funded_expense_monitor' if category == 'self_funded' else 'expense_monitor'
    file_path = _expense_data_file_path()
    upload = request.FILES.get('expense_file')
    if not upload:
        messages.error(request, '请选择要上传的Excel文件。')
        return redirect(redirect_name)

    ext = Path(upload.name).suffix.lower()
    if ext != '.xlsx':
        messages.error(request, '月度支出明细目前仅支持 .xlsx 文件。')
        return redirect(redirect_name)

    temporary_path = None
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix='.expense-upload-',
            suffix='.xlsx',
            dir=str(file_path.parent),
        )
        os.close(fd)
        temporary_path = Path(temporary_name)
        with temporary_path.open('wb') as handle:
            for chunk in upload.chunks():
                handle.write(chunk)
        sheet_name = inspect_expense_workbook(temporary_path)
        os.replace(temporary_path, file_path)
        temporary_path = None
        request.session['expense_original_filename'] = Path(upload.name).name[:255]
        messages.success(
            request,
            f'月度支出明细已上传，已识别工作表“{sheet_name}”；系统将按6606和“期初+本年累计借方-本年累计贷方”生成快照。',
        )
    except ExpenseWorkbookError as exc:
        messages.error(request, f'上传文件未生效：{exc}')
    except Exception as exc:
        messages.error(request, f'上传失败：{exc}')
    finally:
        if temporary_path is not None and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass
    return redirect(redirect_name)


@require_POST
def expense_mapping_view(request):
    category = _valid_funding_category(request.POST.get('funding_category'))
    redirect_name = 'self_funded_expense_monitor' if category == 'self_funded' else 'expense_monitor'
    description = request.POST.get('description', '').strip()
    project_id = request.POST.get('project_id', '').strip()
    if not description or not project_id:
        messages.error(request, '请填写描述并选择匹配课题。')
        return redirect(redirect_name)

    project = get_object_or_404(Project, project_id=project_id)
    normalized = normalized_expense_description(description)[:512]
    if not normalized:
        messages.error(request, '描述无法转换成匹配文本，请检查输入。')
        return redirect(redirect_name)

    ExpenseMapping.objects.update_or_create(
        normalized_text=normalized,
        defaults={
            'description_text': description,
            'project': project,
        }
    )
    messages.success(request, f'已将同名及自筹/专项后缀变体统一映射到：{project.name}')
    redirect_url = reverse(redirect_name)
    if category == 'self_funded':
        redirect_url = f'{redirect_url}?funding_category=self_funded'
    threshold = request.POST.get('threshold', '').strip()
    unmatched_page = request.POST.get('unmatched_page', '').strip()
    if threshold:
        redirect_url = f"{redirect_url}?threshold={threshold}"
    if unmatched_page.isdigit():
        separator = '&' if '?' in redirect_url else '?'
        redirect_url = f"{redirect_url}{separator}unmatched_page={unmatched_page}#unmatched-records"
    return redirect(redirect_url)


SPECIAL_LEDGER_ACTIVE_STATUSES = ('在研', '延期')
SPECIAL_LEDGER_HIGH_RATE = Decimal('90')


def _decimal_or_none(value):
    if value is None or value == '':
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _special_ledger_store_path(ledger_type, original_name):
    safe_name = re.sub(r'[^\w.\-]+', '_', Path(original_name).name)[:120] or 'ledger.xlsx'
    configured = getattr(settings, 'SPECIAL_LEDGER_DIR', None)
    base = Path(configured) if configured else Path(settings.BASE_DIR) / 'zichouktfeiyong' / '专项台账'
    return base / f'{ledger_type}_{safe_name}'


def _special_ledger_assignment_lookup():
    return {
        (assignment.ledger_type, assignment.normalized_name): assignment.project
        for assignment in SpecialLedgerAssignment.objects.select_related('project')
    }


def _special_ledger_rows(import_log, previous_import):
    """把台账明细装配成面板行，并按系统课题状态标注。

    专项经费一律取课题档案登记的额度（外部专项 / 院专项），台账只提供已执行与本年收入。
    """
    previous_executed = {}
    if previous_import is not None:
        previous_executed = {
            row.ledger_name: row.executed_total
            for row in previous_import.rows.exclude(executed_total=None)
        }

    rows = []
    for row in import_log.rows.select_related('project').all():
        project = row.project
        system_status = project.status if project else ''
        is_active = bool(project) and system_status in SPECIAL_LEDGER_ACTIVE_STATUSES

        special_budget = None
        if project is not None:
            value = getattr(project, LEDGER_SPECIAL_BUDGET_FIELD[import_log.ledger_type])
            special_budget = Decimal(str(value)) if value is not None else None
        executed = row.executed_total
        remaining = None
        rate = None
        if special_budget is not None:
            remaining = special_budget - (executed or Decimal('0'))
            if special_budget:
                rate = (executed or Decimal('0')) / special_budget * Decimal('100')

        previous_total = previous_executed.get(row.ledger_name)
        increase = None
        if previous_total is not None and executed is not None:
            increase = executed - previous_total

        rows.append({
            'row': row,
            'project': project,
            'is_active': is_active,
            'system_status': system_status,
            'special_budget': special_budget,
            'executed': executed,
            'remaining': remaining,
            'rate': rate,
            'unreceived': row.unreceived_amount,
            'year_executed': row.year_executed,
            'previous_total': previous_total,
            'increase': increase,
            'ledger_project_mismatch': bool(project) and row.ledger_status
            and row.ledger_status not in {'——', system_status},
        })
    return rows


def special_expense_monitor_view(request):
    """专项经费面板：只看专项课题的外部专项与院专项执行情况。"""
    ledger_type = str(request.GET.get('ledger_type', '')).strip()
    if ledger_type not in dict(SpecialLedgerImport.LEDGER_TYPE_CHOICES):
        ledger_type = ''

    imports = {}
    previous_imports = {}
    for candidate, _label in SpecialLedgerImport.LEDGER_TYPE_CHOICES:
        if ledger_type and candidate != ledger_type:
            continue
        history = list(SpecialLedgerImport.objects.filter(ledger_type=candidate).order_by('-created_at'))
        if history:
            imports[candidate] = history[0]
            previous_imports[candidate] = history[1] if len(history) > 1 else None

    active_rows = []
    closed_rows = []
    pending_rows = []
    for candidate, import_log in imports.items():
        for entry in _special_ledger_rows(import_log, previous_imports.get(candidate)):
            state = entry['row'].match_state
            if state == 'ambiguous':
                entry['candidates'] = list(
                    Project.objects.filter(name=entry['row'].ledger_name).order_by('project_id')
                )
                pending_rows.append(entry)
            elif entry['is_active']:
                active_rows.append(entry)
            else:
                closed_rows.append(entry)

    def sort_key(entry):
        row = entry['row']
        return (-(row.executed_total or Decimal('0')), row.ledger_name)

    active_rows.sort(key=sort_key)
    closed_rows.sort(key=sort_key)

    def summarize(entries):
        budget = Decimal('0')
        executed = Decimal('0')
        remaining = Decimal('0')
        year_executed = Decimal('0')
        for entry in entries:
            if entry['special_budget'] is not None:
                budget += entry['special_budget']
            if entry['executed'] is not None:
                executed += entry['executed']
            if entry['remaining'] is not None:
                remaining += entry['remaining']
            if entry['year_executed'] is not None:
                year_executed += entry['year_executed']
        return {
            'count': len(entries),
            'budget': budget,
            'executed': executed,
            'remaining': remaining,
            'year_executed': year_executed,
            'rate': (executed / budget * Decimal('100')) if budget else None,
        }

    risk_rows = [
        entry for entry in active_rows
        if (entry['remaining'] is not None and entry['remaining'] < 0)
        or (entry['rate'] is not None and entry['rate'] >= SPECIAL_LEDGER_HIGH_RATE)
        or (entry['unreceived'] is not None and entry['unreceived'] < 0)
    ]
    growth_rows = [
        entry for entry in active_rows
        if entry['increase'] is not None and entry['increase'] > 0
    ]

    ignored_rows = []
    for candidate, import_log in imports.items():
        for unmatched in import_log.ignored_detail or []:
            unmatched = dict(unmatched)
            unmatched['ledger_type_label'] = import_log.get_ledger_type_display()
            unmatched['executed_amount'] = _decimal_or_none(unmatched.get('executed_total'))
            ignored_rows.append(unmatched)
    ignored_rows.sort(key=lambda item: -(item['executed_amount'] or Decimal('0')))

    context = {
        'imports': imports,
        'ledger_type': ledger_type,
        'ledger_type_options': SpecialLedgerImport.LEDGER_TYPE_CHOICES,
        'active_rows': active_rows,
        'closed_rows': closed_rows,
        'pending_rows': pending_rows,
        'risk_rows': risk_rows,
        'growth_rows': growth_rows,
        'ignored_rows': ignored_rows,
        'active_summary': summarize(active_rows),
        'closed_summary': summarize(closed_rows),
        'is_system_admin': is_system_admin(request.user),
        'unit_label': EXPENSE_UNIT_LABEL,
        'active_statuses': SPECIAL_LEDGER_ACTIVE_STATUSES,
        'high_rate': SPECIAL_LEDGER_HIGH_RATE,
    }
    return render(request, 'core/special_expense_monitor.html', context)


@require_POST
def special_ledger_import_view(request):
    ledger_type = str(request.POST.get('ledger_type', '')).strip()
    if ledger_type not in dict(SpecialLedgerImport.LEDGER_TYPE_CHOICES):
        messages.error(request, '请选择正确的台账类别。')
        return redirect('special_expense_monitor')

    upload = request.FILES.get('ledger_file')
    if not upload:
        messages.error(request, '请选择要上传的台账文件。')
        return redirect('special_expense_monitor')
    if Path(upload.name).suffix.lower() not in {'.xlsx', '.xlsm'}:
        messages.error(request, '专项经费台账仅支持 .xlsx 文件。')
        return redirect('special_expense_monitor')

    store_path = _special_ledger_store_path(ledger_type, upload.name)
    temporary_path = None
    try:
        store_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix='.special-ledger-', suffix='.xlsx', dir=str(store_path.parent))
        os.close(fd)
        temporary_path = Path(temporary_name)
        with temporary_path.open('wb') as handle:
            for chunk in upload.chunks():
                handle.write(chunk)

        result = parse_special_ledger(
            temporary_path,
            ledger_type,
            projects=list(Project.objects.all()),
            assignment_lookup=_special_ledger_assignment_lookup(),
        )
        file_hash = file_sha256(temporary_path)
        file_mtime = datetime.fromtimestamp(
            temporary_path.stat().st_mtime,
            tz=timezone.get_current_timezone(),
        )
        os.replace(temporary_path, store_path)
        temporary_path = None

        with transaction.atomic():
            import_log, created = SpecialLedgerImport.objects.get_or_create(
                ledger_type=ledger_type,
                file_sha256=file_hash,
                defaults={
                    'source_file': str(store_path),
                    'original_filename': Path(upload.name).name[:255],
                    'sheet_name': result['sheet_name'],
                    'row_total': result['row_total'],
                    'matched_total': result['matched_total'],
                    'ambiguous_total': result['ambiguous_total'],
                    'ignored_total': result['ignored_total'],
                    'totals': {key: str(value) for key, value in result['totals'].items() if value is not None},
                    'ignored_detail': [
                        {
                            'ledger_name': item['ledger_name'],
                            'ledger_status': item['ledger_status'],
                            'funder': item['funder'],
                            'executed_total': str(item['executed_total']) if item['executed_total'] is not None else None,
                            'reason': item['reason'],
                        }
                        for item in result['ignored_rows']
                    ],
                    'file_mtime': file_mtime,
                    'created_by': request.user if request.user.is_authenticated else None,
                },
            )
            if created:
                rows = [
                    SpecialLedgerRow(
                        import_log=import_log,
                        ledger_type=ledger_type,
                        row_number=record['row_number'],
                        sequence=record['sequence'][:50],
                        project=record['project'],
                        ledger_name=record['ledger_name'][:255],
                        owning_unit=record['owning_unit'][:100],
                        funder=record['funder'][:100],
                        ledger_status=record['ledger_status'][:50],
                        principal=record['principal'][:50],
                        start_text=record['start_text'][:50],
                        end_text=record['end_text'][:50],
                        match_state=record['match_state'],
                        **{
                            field_name: record.get(field_name)
                            for field_name in (
                                'contract_total', 'contract_allocated', 'received_amount', 'approved_budget',
                                'executed_total', 'remaining_amount', 'year_disposable', 'year_budget', 'year_executed',
                            )
                        },
                    )
                    for record in result['records']
                ]
                SpecialLedgerRow.objects.bulk_create(rows)

        if created:
            messages.success(
                request,
                f'{import_log.get_ledger_type_display()}台账已导入：关联 {result["matched_total"]} 个系统课题，'
                f'{result["ambiguous_total"]} 个重名待确认，忽略 {result["ignored_total"]} 个系统外课题。',
            )
        else:
            messages.info(request, '该台账文件此前已导入，展示的还是同一批数据。')
    except SpecialLedgerError as exc:
        messages.error(request, f'导入未生效：{exc}')
    except Exception as exc:
        messages.error(request, f'导入失败：{exc}')
    finally:
        if temporary_path is not None and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass
    return redirect('special_expense_monitor')


@require_POST
def special_ledger_assign_view(request):
    ledger_type = str(request.POST.get('ledger_type', '')).strip()
    ledger_name = str(request.POST.get('ledger_name', '')).strip()
    project_id = str(request.POST.get('project_id', '')).strip()
    if ledger_type not in dict(SpecialLedgerImport.LEDGER_TYPE_CHOICES) or not ledger_name or not project_id:
        messages.error(request, '请选择台账类别并指定对应课题。')
        return redirect('special_expense_monitor')

    project = Project.objects.filter(project_id=project_id).first()
    if project is None:
        messages.error(request, f'系统里没有课题编号为 {project_id} 的课题，请核对后再提交。')
        return redirect('special_expense_monitor')

    normalized_name = normalize_project_name(ledger_name)
    SpecialLedgerAssignment.objects.update_or_create(
        ledger_type=ledger_type,
        normalized_name=normalized_name,
        defaults={
            'project': project,
            'ledger_name': ledger_name[:255],
            'created_by': request.user if request.user.is_authenticated else None,
        },
    )
    latest = SpecialLedgerImport.objects.filter(ledger_type=ledger_type).order_by('-created_at').first()
    if latest is not None:
        latest.rows.filter(ledger_name=ledger_name).update(project=project, match_state='manual')
    messages.success(request, f'已将台账课题“{ledger_name}”对应到：{project.name}')
    return redirect('special_expense_monitor')


def delete_project_view(request, project_id):
    project = get_object_or_404(Project, project_id=project_id)
    if request.method == 'POST':
        if project.directory_path and os.path.exists(project.directory_path):
            try:
                shutil.rmtree(project.directory_path)
            except OSError as e:
                messages.error(request, f"删除文件夹失败: {e}")
                return redirect('project_detail', project_id=project.project_id)
        project.delete()
        messages.success(request, f"项目 {project.name} 已被成功删除。")
        return redirect('project_list')
    return render(request, 'core/delete_project.html', {'project': project})

def _metric_identity(value):
    return re.sub(r'[\s，。；：、,.;:（）()\-_]+', '', value or '').lower()


def _normalize_metric_target(value, unit=''):
    """把纯数字数量补成指标表里的常用写法，同时保留用户输入的复合目标。"""
    normalized = str(value or '').strip()
    if normalized and unit and re.fullmatch(r'\d+(?:\.\d+)?', normalized):
        return f'{normalized}{unit}'
    return normalized


def _sync_metrics_items(analysis, extracted_items, extraction_source='ai'):
    """同步任务书指标，更新来源字段但保留已填完成情况和佐证文件。"""
    existing_items = list(analysis.metrics_items.all())
    existing_by_name = {
        _metric_identity(item.item_name): item
        for item in existing_items
        if _metric_identity(item.item_name)
    }
    created_count = 0
    updated_count = 0
    valid_categories = dict(MetricsItem.CATEGORY_CHOICES)

    for order, item_data in enumerate(extracted_items, start=1):
        item_name = str(item_data.get('item_name') or '').strip()
        if not item_name:
            continue
        catalog_item = MetricsItem.get_catalog_item(item_name)
        identity = _metric_identity(item_name)
        metric = existing_by_name.get(identity)
        defaults = {
            'category': (
                catalog_item['category'] if catalog_item
                else item_data.get('category') if item_data.get('category') in valid_categories
                else 'other'
            ),
            'item_name': item_name[:255],
            'target_value': _normalize_metric_target(
                item_data.get('target_value'),
                catalog_item['unit'] if catalog_item else '',
            )[:100],
            'assessment_method': str(
                item_data.get('assessment_method')
                or (catalog_item['assessment_method'] if catalog_item else '')
            )[:255],
            'planned_period': str(item_data.get('planned_period') or '')[:100],
            'source_section': str(item_data.get('source_section') or '')[:100],
            'source_page': str(item_data.get('source_page') or '')[:50],
            'sort_order': int(item_data.get('sort_order') or order),
        }
        parsed_deadline = parse_date(str(item_data.get('deadline') or ''))
        if parsed_deadline:
            defaults['deadline'] = parsed_deadline

        source_notes = str(item_data.get('notes') or '').strip()
        if metric:
            for field_name, value in defaults.items():
                setattr(metric, field_name, value)
            if source_notes and not metric.notes:
                metric.notes = source_notes
            metric.save()
            updated_count += 1
        else:
            metric = MetricsItem.objects.create(
                analysis=analysis,
                current_value='',
                status='pending',
                progress_percent=0,
                notes=source_notes,
                extraction_source=extraction_source,
                **defaults,
            )
            existing_by_name[identity] = metric
            created_count += 1
    return created_count, updated_count


def _metrics_redirect(project_id):
    base_url = reverse('project_detail', kwargs={'project_id': project_id})
    return redirect(f'{base_url}?tab=metrics-analysis')


def project_detail_view(request, project_id):
    project = get_object_or_404(Project, project_id=project_id)
    
    if request.method == 'POST' and 'document' not in request.FILES:
        form = ProjectForm(request.POST, instance=project)
        if form.is_valid():
            # Check if folder-naming fields have changed
            old_project_instance = Project.objects.get(pk=project_id)
            old_path = old_project_instance.directory_path
            
            updated_project = form.save(commit=False)
            
            if (old_project_instance.status != updated_project.status or 
                old_project_instance.start_year != updated_project.start_year or 
                old_project_instance.name != updated_project.name):
                rename_project_folder(request, updated_project, old_path)
            
            updated_project.save()
            form.save_m2m() # Save many-to-many fields if any

            return redirect('project_detail', project_id=updated_project.project_id)
    else:
        form = ProjectForm(instance=project)

    # 确保项目目录存在并获取文件树
    file_tree = []
    try:
        # 目录路径由 _get_project_folder_name 唯一决定；此处只做一次纠偏与补建，
        # 不再按编号子串猜测旧目录（会误改他人目录），改名统一走 create_project_directory_structure。
        expected_path = os.path.join(str(settings.PROJECTS_ROOT), _get_project_folder_name(project))
        if project.directory_path != expected_path:
            project.directory_path = expected_path
            project.save(update_fields=['directory_path'])

        if not os.path.isdir(expected_path):
            create_project_directory_structure(project)
            project.refresh_from_db()

        file_tree = get_directory_level(expected_path, expected_path)
        if not file_tree:
            messages.info(request, "项目目录结构已创建，但暂无文件。您可以通过右侧文件操作面板上传文件。")

    except Exception as e:
        messages.error(request, f"处理项目目录时出错: {e}")

    # Calculate relative path for file uploads
    relative_directory_path = ""
    
    # Get analysis results
    research_analysis = project.analyses.filter(analysis_type='research_content').first()
    metrics_analysis = project.analyses.filter(analysis_type='output_metrics').first()
    
    # 按任务书分类组织指标，并计算完成概览和佐证文件可用性。
    metrics_items_by_category = {}
    metrics_summary = {
        'total': 0,
        'completed': 0,
        'in_progress': 0,
        'overdue': 0,
        'average_progress': 0,
        'evidence_count': 0,
    }
    if metrics_analysis:
        metrics_items = list(
            metrics_analysis.metrics_items.prefetch_related('evidence_files').all()
        )
        today = timezone.localdate()
        for item in metrics_items:
            item.is_overdue = bool(item.deadline and item.deadline < today and item.status != 'completed')
            for evidence in item.evidence_files.all():
                evidence_abs_path = _build_abs_path(project.directory_path, evidence.relative_path)
                evidence.is_available = bool(
                    _is_within_root(evidence_abs_path, project.directory_path)
                    and os.path.isfile(evidence_abs_path)
                )
            category_display = item.get_configured_category_display()
            if category_display not in metrics_items_by_category:
                metrics_items_by_category[category_display] = []
            metrics_items_by_category[category_display].append(item)
        metrics_summary['total'] = len(metrics_items)
        metrics_summary['completed'] = sum(item.status == 'completed' for item in metrics_items)
        metrics_summary['in_progress'] = sum(item.status == 'in_progress' for item in metrics_items)
        metrics_summary['overdue'] = sum(item.is_overdue for item in metrics_items)
        metrics_summary['evidence_count'] = sum(len(item.evidence_files.all()) for item in metrics_items)
        if metrics_items:
            metrics_summary['average_progress'] = round(
                sum(item.progress_percent for item in metrics_items) / len(metrics_items)
            )
    
    # Get API configuration status
    api_configs = APIConfig.objects.filter(is_active=True)
    available_ai_configs = ready_configs()
    api_status = {
        'has_config': api_configs.exists(),
        'deepseek_available': api_configs.filter(service_name='deepseek', test_success=True).exists(),
        'kimi_available': api_configs.filter(service_name='kimi', test_success=True).exists(),
        'local_available': api_configs.filter(service_name='local', test_success=True).exists(),
        'ready_configs': available_ai_configs,
        'has_ready_config': bool(available_ai_configs),
        'total_configs': api_configs.count()
    }

    network_config = get_network_config()
    protocol_setup_reg_path = str(Path(settings.BASE_DIR) / 'setup_protocol_handler.reg')
    client_setup_reg_path = str(Path(settings.BASE_DIR) / 'client_setup_protocol.reg')

    # 读取tab参数，用于分析完成后返回正确的tab
    active_tab = request.GET.get('tab', 'info')
    valid_tabs = {'info', 'files', 'content-analysis', 'metrics-analysis'}
    if active_tab not in valid_tabs:
        active_tab = 'info'

    context = {
        'project': project,
        'form': form,
        'file_tree': file_tree,
        'research_analysis': research_analysis,
        'metrics_analysis': metrics_analysis,
        'metrics_items_by_category': metrics_items_by_category,
        'metrics_summary': metrics_summary,
        'metric_category_choices': MetricsItem.CATEGORY_CHOICES,
        'metric_indicator_catalog': MetricsItem.get_indicator_catalog(),
        'api_status': api_status,
        'relative_directory_path': relative_directory_path,
        'enable_network_share': network_config.get('enable_network_share', False),
        'network_share_path': network_config.get('network_share_path', ''),
        'enable_web_file_trial': network_config.get('enable_web_file_trial', True),
        'protocol_setup_reg_path': protocol_setup_reg_path,
        'client_setup_reg_path': client_setup_reg_path,
        'active_tab': active_tab,
        **_funding_context(project.funding_category),
    }
    return render(request, 'core/project_detail.html', context)

def file_manager_trial_view(request, project_id):
    """Web 文件管理试用页，不影响现有模式。"""
    project = get_object_or_404(Project, project_id=project_id)
    network_config = get_network_config()

    if not network_config.get('enable_web_file_trial', True):
        messages.warning(request, 'Web 文件管理试用功能当前已关闭。')
        return redirect('project_detail', project_id=project_id)

    create_project_directory_structure(project)
    project.refresh_from_db()

    return render(request, 'core/file_manager_trial.html', {
        'project': project,
    })

def get_file_tree_view(request, project_id):
    """API端点：按需返回项目某一层目录，避免递归扫描大型课题。"""
    project = get_object_or_404(Project, project_id=project_id)
    
    # 确保项目目录存在
    if not project.directory_path or not os.path.isdir(project.directory_path):
        create_project_directory_structure(project)
    
    relative_path = request.GET.get('path', '').strip()
    project_root = os.path.normpath(project.directory_path)
    target_path = os.path.normpath(_build_abs_path(project_root, relative_path))
    if not _is_within_root(target_path, project_root) or not os.path.isdir(target_path):
        return JsonResponse({
            'success': False,
            'message': '无效或不存在的目录路径。',
        }, status=400)

    # 只获取当前目录的一层数据，子目录在展开时继续请求。
    file_tree = []
    if project_root and os.path.exists(project_root):
        file_tree = get_directory_level(target_path, project_root)

    total_files = sum(1 for node in file_tree if node['type'] == 'file')
    total_folders = sum(1 for node in file_tree if node['type'] == 'folder')
    
    return JsonResponse({
        'success': True,
        'path': _relpath_for_tree(target_path, project_root) if relative_path else '',
        'file_tree': file_tree,
        'stats': {
            'file_count': total_files,
            'folder_count': total_folders,
            'total_count': total_files + total_folders
        }
    })

def file_action_view(request, project_id, action):
    project = get_object_or_404(Project, project_id=project_id)
    if not project.directory_path or not os.path.isdir(project.directory_path):
        create_project_directory_structure(project)

    # Check if this is an AJAX request
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    project_root = project.directory_path
    # 删除/改名等入口是普通链接，整页跳转回来必须停在“文件管理”标签，
    # 否则课题详情页默认 active_tab='info'，操作成功反而被甩回课题信息页。
    back_to_files = reverse('project_detail', kwargs={'project_id': project.project_id}) + '?tab=files'

    if action == 'upload' and request.method == 'POST':
        uploaded_files = request.FILES.getlist('files')
        target_path_rel = request.POST.get('target_path', '')

        if not uploaded_files:
            if is_ajax:
                return JsonResponse({'success': False, 'message': '请选择要上传的文件。'})
            messages.error(request, "请选择要上传的文件。")
            return redirect(back_to_files)
        
        target_path_abs = _build_abs_path(project_root, target_path_rel)
        target_path_abs = os.path.normpath(target_path_abs)
        
        if not _is_within_root(target_path_abs, project_root):
            if is_ajax:
                return JsonResponse({'success': False, 'message': '无效的目标路径。'})
            messages.error(request, "无效的目标路径。")
            return redirect(back_to_files)
        
        # 确保目标目录存在
        os.makedirs(target_path_abs, exist_ok=True)
        
        success_count = 0
        error_messages = []
        renamed = []
        
        for uploaded_file in uploaded_files:
            try:
                # 清理文件名，移除危险字符，但保留中文字符
                safe_filename = re.sub(r'[\\/*?"<>|:]', '_', uploaded_file.name)
                # 确保文件名不为空且不以点开头
                if not safe_filename or safe_filename.startswith('.'):
                    safe_filename = f"file_{int(time.time())}{os.path.splitext(uploaded_file.name)[1]}"
                file_path = _unique_upload_path(target_path_abs, safe_filename)

                # 'xb' 独占创建：并发上传撞上同名时直接报错，而不是静默覆盖
                with open(file_path, 'xb') as destination:
                    for chunk in uploaded_file.chunks():
                        destination.write(chunk)
                if os.path.basename(file_path) != safe_filename:
                    renamed.append(f"{uploaded_file.name} → {os.path.basename(file_path)}")
                success_count += 1
            except Exception as e:
                error_messages.append(f"{uploaded_file.name}: {str(e)}")
        
        if success_count > 0:
            success_message = f"成功上传 {success_count} 个文件。"
            if renamed:
                success_message += f" 同名文件已另存为: {'; '.join(renamed)}"
            if error_messages:
                success_message += f" 失败: {'; '.join(error_messages)}"

            if is_ajax:
                return JsonResponse({'success': True, 'message': success_message})
            messages.success(request, success_message)
        else:
            error_message = f"文件上传失败: {'; '.join(error_messages)}"
            if is_ajax:
                return JsonResponse({'success': False, 'message': error_message})
            messages.error(request, error_message)

    elif action == 'delete':
        item_path_rel = request.POST.get('path') or request.GET.get('path')
        if item_path_rel:
            item_path_abs = _resolve_within_root(project_root, item_path_rel)

            if item_path_abs is None:
                if is_ajax:
                    return JsonResponse({'success': False, 'message': '无效的文件路径：只能删除课题目录内的文件或文件夹。'})
                messages.error(request, "无效的文件路径：只能删除课题目录内的文件或文件夹。")
                return redirect(back_to_files)

            if os.path.isdir(item_path_abs) and _is_protected_folder(item_path_abs):
                if is_ajax:
                    return JsonResponse({'success': False, 'message': '标准目录（01~06）不允许删除。'})
                messages.error(request, "标准目录（01~06）不允许删除。")
                return redirect(back_to_files)
                
            if os.path.exists(item_path_abs):
                try:
                    if os.path.isdir(item_path_abs):
                        shutil.rmtree(item_path_abs)
                        if is_ajax:
                            return JsonResponse({'success': True, 'message': '文件夹删除成功！'})
                        messages.success(request, "文件夹删除成功！")
                    else:
                        os.remove(item_path_abs)
                        if is_ajax:
                            return JsonResponse({'success': True, 'message': '文件删除成功！'})
                        messages.success(request, "文件删除成功！")
                except OSError as e:
                    if is_ajax:
                        return JsonResponse({'success': False, 'message': f'删除失败: {e}'})
                    messages.error(request, f"删除失败: {e}")
            else:
                if is_ajax:
                    return JsonResponse({'success': False, 'message': '文件或文件夹不存在。'})
                messages.error(request, "文件或文件夹不存在。")

    elif action == 'rename' and request.method == 'POST':
        item_path_rel = request.POST.get('path', '')
        new_name = request.POST.get('new_name', '').strip()

        if not item_path_rel:
            if is_ajax:
                return JsonResponse({'success': False, 'message': '缺少目标路径。'})
            messages.error(request, "缺少目标路径。")

        if not new_name:
            if is_ajax:
                return JsonResponse({'success': False, 'message': '新名称不能为空。'})
            messages.error(request, "新名称不能为空。")
            return redirect(back_to_files)

        safe_name = re.sub(r'[\\/*?"<>|:]', '_', new_name)
        if safe_name in ('.', '..'):
            if is_ajax:
                return JsonResponse({'success': False, 'message': '新名称不合法。'})
            messages.error(request, "新名称不合法。")
            return redirect(back_to_files)

        item_path_abs = _resolve_within_root(project_root, item_path_rel)
        if item_path_abs is None:
            if is_ajax:
                return JsonResponse({'success': False, 'message': '无效的目标路径：只能重命名课题目录内的文件或文件夹。'})
            messages.error(request, "无效的目标路径：只能重命名课题目录内的文件或文件夹。")
            return redirect(back_to_files)

        # 把 01~06 改名等同于删掉标准目录（下次打开只会新建空目录，资料被孤立）
        if os.path.isdir(item_path_abs) and _is_protected_folder(item_path_abs):
            if is_ajax:
                return JsonResponse({'success': False, 'message': '标准目录（01~06）不允许重命名。'})
            messages.error(request, "标准目录（01~06）不允许重命名。")
            return redirect(back_to_files)

        if not os.path.exists(item_path_abs):
            if is_ajax:
                return JsonResponse({'success': False, 'message': '目标不存在。'})
            messages.error(request, "目标不存在。")
            return redirect(back_to_files)

        parent_dir = os.path.dirname(item_path_abs)
        new_path_abs = os.path.normpath(os.path.join(parent_dir, safe_name))
        if not _is_within_root(new_path_abs, project_root):
            if is_ajax:
                return JsonResponse({'success': False, 'message': '重命名目标非法。'})
            messages.error(request, "重命名目标非法。")
            return redirect(back_to_files)

        if os.path.exists(new_path_abs):
            if is_ajax:
                return JsonResponse({'success': False, 'message': '同名文件或文件夹已存在。'})
            messages.error(request, "同名文件或文件夹已存在。")
            return redirect(back_to_files)

        try:
            os.rename(item_path_abs, new_path_abs)
            if is_ajax:
                return JsonResponse({'success': True, 'message': '重命名成功。'})
            messages.success(request, "重命名成功。")
        except OSError as e:
            if is_ajax:
                return JsonResponse({'success': False, 'message': f'重命名失败: {e}'})
            messages.error(request, f"重命名失败: {e}")
    
    elif action == 'download':
        item_path_rel = request.GET.get('path')
        if item_path_rel:
            item_path_abs = _build_abs_path(project_root, item_path_rel)
            item_path_abs = os.path.normpath(item_path_abs)
            
            if not _is_within_root(item_path_abs, project_root):
                messages.error(request, "无效的文件路径。")
                return redirect(back_to_files)
                
            if os.path.exists(item_path_abs) and os.path.isfile(item_path_abs):
                try:
                    response = FileResponse(
                        open(item_path_abs, 'rb'),
                        as_attachment=True,
                        filename=force_str(os.path.basename(item_path_abs))
                    )
                    return response
                except Exception as e:
                    messages.error(request, f"文件下载失败: {e}")
            else:
                messages.error(request, "文件不存在。")
    
    elif action == 'preview':
        item_path_rel = request.GET.get('path')
        if item_path_rel:
            item_path_abs = _build_abs_path(project_root, item_path_rel)
            item_path_abs = os.path.normpath(item_path_abs)
            
            if not _is_within_root(item_path_abs, project_root):
                return HttpResponse("无效的文件路径", status=403)
                
            if os.path.exists(item_path_abs) and os.path.isfile(item_path_abs):
                try:
                    # Get file content type
                    content_type, _ = mimetypes.guess_type(item_path_abs)
                    
                    # For text files, try to read and display content
                    if content_type and content_type.startswith('text/'):
                        try:
                            with open(item_path_abs, 'r', encoding='utf-8') as f:
                                content = f.read()
                            return HttpResponse(content, content_type='text/plain; charset=utf-8')
                        except UnicodeDecodeError:
                            # Try with other encodings
                            try:
                                with open(item_path_abs, 'r', encoding='gbk') as f:
                                    content = f.read()
                                return HttpResponse(content, content_type='text/plain; charset=utf-8')
                            except UnicodeDecodeError:
                                return HttpResponse("无法解码文件内容", status=400)
                    
                    # For images, return the file directly
                    elif content_type and content_type.startswith('image/'):
                        return FileResponse(open(item_path_abs, 'rb'), content_type=content_type)
                    
                    # For PDFs, return the file directly
                    elif content_type == 'application/pdf':
                        return FileResponse(open(item_path_abs, 'rb'), content_type=content_type)
                    
                    else:
                        return HttpResponse(f"不支持预览此文件类型: {content_type or '未知类型'}", status=400)
                        
                except Exception as e:
                    return HttpResponse(f"文件预览失败: {e}", status=500)
            else:
                return HttpResponse("文件不存在", status=404)
    
    elif action == 'create_folder' and request.method == 'POST':
        folder_name = request.POST.get('folder_name', '').strip()
        parent_path_rel = request.POST.get('parent_path', '')
        
        if not folder_name:
            if is_ajax:
                return JsonResponse({'success': False, 'message': '文件夹名称不能为空。'})
            messages.error(request, "文件夹名称不能为空。")
            return redirect(back_to_files)
        
        # Sanitize folder name
        folder_name = re.sub(r'[\\/*?"<>|]', '_', folder_name)
        
        parent_path_abs = _build_abs_path(project_root, parent_path_rel)
        parent_path_abs = os.path.normpath(parent_path_abs)
        
        if not _is_within_root(parent_path_abs, project_root):
            if is_ajax:
                return JsonResponse({'success': False, 'message': '无效的父目录路径。'})
            messages.error(request, "无效的父目录路径。")
            return redirect(back_to_files)
        
        new_folder_path = os.path.join(parent_path_abs, folder_name)
        
        if os.path.exists(new_folder_path):
            if is_ajax:
                return JsonResponse({'success': False, 'message': f'文件夹 \'{folder_name}\' 已存在。'})
            messages.error(request, f"文件夹 '{folder_name}' 已存在。")
        else:
            try:
                os.makedirs(new_folder_path, exist_ok=True)
                if is_ajax:
                    return JsonResponse({'success': True, 'message': f'文件夹 \'{folder_name}\' 创建成功！'})
                messages.success(request, f"文件夹 '{folder_name}' 创建成功！")
            except Exception as e:
                if is_ajax:
                    return JsonResponse({'success': False, 'message': f'创建文件夹失败: {e}'})
                messages.error(request, f"创建文件夹失败: {e}")

    return redirect(back_to_files)

def analyze_content_view(request, project_id, analysis_type):
    from .ai_analysis import ai_service
    
    project = get_object_or_404(Project, project_id=project_id)
    if analysis_type not in dict(ProjectAnalysis.ANALYSIS_TYPE_CHOICES):
        messages.error(request, '无效的分析类型。')
        return redirect('project_detail', project_id=project.project_id)
    
    # 根据分析类型确定返回时的tab锚点
    tab_anchor_map = {
        'research_content': 'content-analysis',
        'output_metrics': 'metrics-analysis',
    }
    tab_anchor = tab_anchor_map.get(analysis_type, '')

    if request.method == 'POST':
        # 兼容前端表单字段名 'files' 和 'document'
        uploaded_file = request.FILES.get('files') or request.FILES.get('document')
        
        if uploaded_file:
            # 使用AI分析服务处理文档
            service_name = (request.POST.get('ai_service') or '').strip()
            result = ai_service.analyze_document(
                uploaded_file,
                analysis_type,
                service_name=service_name or None,
            )
            
            if result['success']:
                with transaction.atomic():
                    analysis, created = ProjectAnalysis.objects.update_or_create(
                        project=project,
                        analysis_type=analysis_type,
                        defaults={
                            'file_name': uploaded_file.name,
                            'file_size': uploaded_file.size,
                            'analysis_result': result['result'],
                            'structured_data': result.get('structured_data') or {},
                            'confidence_score': result.get('confidence_score'),
                            'processing_time': result['processing_time'],
                        }
                    )
                    created_metrics = updated_metrics = 0
                    if analysis_type == 'output_metrics':
                        created_metrics, updated_metrics = _sync_metrics_items(
                            analysis,
                            result.get('metrics') or [],
                            extraction_source='ai',
                        )

                action = '创建' if created else '更新'
                api_info = f" (使用{result.get('api_used', 'AI')}服务)" if result.get('api_used') else ""
                metric_info = ''
                if analysis_type == 'output_metrics':
                    metric_info = f' 新增 {created_metrics} 项、刷新 {updated_metrics} 项，原有完成记录和佐证文件均已保留。'
                messages.success(
                    request,
                    f'文档分析完成！{action}了{analysis.get_analysis_type_display()}结果。{metric_info}{api_info}',
                )
            else:
                # 分析失败，显示错误信息
                messages.error(request, f'文档分析失败: {result["error"]}')
        else:
            messages.error(request, '请选择要分析的文件。')
    
    # 带上tab参数，确保返回后停留在正确的分析tab
    base_url = reverse('project_detail', kwargs={'project_id': project.project_id})
    return redirect(f'{base_url}?tab={tab_anchor}')

def parse_metrics_analysis(analysis_text):
    """兼容旧调用：优先解析任务书结构化 JSON，并兼容历史 Markdown。"""
    return parse_metrics_text(analysis_text)


def edit_analysis_view(request, project_id, analysis_type):
    """编辑分析结果的视图"""
    project = get_object_or_404(Project, project_id=project_id)
    if analysis_type not in dict(ProjectAnalysis.ANALYSIS_TYPE_CHOICES):
        messages.error(request, '无效的分析类型。')
        return redirect('project_detail', project_id=project.project_id)
    
    if request.method == 'POST':
        action = request.POST.get('action', 'save')
        analysis_result = request.POST.get('analysis_result', '').strip()

        # 研究内容允许从页面明确删除；清空编辑框后保存也按删除处理，
        # 避免旧 structured_data 继续把已删除的内容渲染出来。
        if analysis_type == 'research_content' and (action == 'delete' or not analysis_result):
            with transaction.atomic():
                deleted_count, _ = ProjectAnalysis.objects.filter(
                    project=project,
                    analysis_type=analysis_type,
                ).delete()
                Project.objects.filter(pk=project.pk).update(research_content='')

            if deleted_count:
                messages.success(request, '研究内容分析结果已删除。')
            else:
                messages.info(request, '当前没有可删除的研究内容分析结果。')
        elif action == 'delete':
            messages.error(request, '该分析类型不支持在此删除。')
        
        elif analysis_result:
            display_result, structured_data, metrics_items = parse_ai_analysis(
                analysis_result,
                analysis_type,
            )
            with transaction.atomic():
                analysis, created = ProjectAnalysis.objects.update_or_create(
                    project=project,
                    analysis_type=analysis_type,
                    defaults={
                        'file_name': '手动编辑',
                        'file_size': None,
                        'analysis_result': display_result,
                        'structured_data': structured_data,
                        'confidence_score': None,
                        'processing_time': None,
                    }
                )
                created_metrics = updated_metrics = 0
                if analysis_type == 'output_metrics':
                    created_metrics, updated_metrics = _sync_metrics_items(
                        analysis,
                        metrics_items,
                        extraction_source='manual',
                    )

            if analysis_type == 'output_metrics':
                messages.success(
                    request,
                    f'手动{"创建" if created else "更新"}了{analysis.get_analysis_type_display()}结果，'
                    f'新增 {created_metrics} 项、刷新 {updated_metrics} 项；已有完成情况和佐证文件未被删除。',
                )
            else:
                messages.success(request, f'手动{"创建" if created else "更新"}了{analysis.get_analysis_type_display()}结果。')
        else:
            messages.error(request, '分析结果内容不能为空。')
    
    # 带上tab参数，确保返回后停留在正确的分析tab
    tab_anchor_map = {
        'research_content': 'content-analysis',
        'output_metrics': 'metrics-analysis',
    }
    tab_anchor = tab_anchor_map.get(analysis_type, '')
    base_url = reverse('project_detail', kwargs={'project_id': project.project_id})
    return redirect(f'{base_url}?tab={tab_anchor}')


def update_metrics_item_view(request, project_id, item_id):
    """更新指标目标、考核方式及完成情况。"""
    if request.method == 'POST':
        item = get_object_or_404(MetricsItem, id=item_id, analysis__project__project_id=project_id)
        status = request.POST.get('status', item.status)
        if status not in dict(MetricsItem.STATUS_CHOICES):
            messages.error(request, '无效的状态值。')
            return _metrics_redirect(project_id)

        requested_item_name = request.POST.get('item_name', item.item_name).strip()[:255] or item.item_name
        catalog_item = MetricsItem.get_catalog_item(requested_item_name)
        if not catalog_item and requested_item_name != item.item_name:
            messages.error(request, '具体指标必须从指标清单中选择。')
            return _metrics_redirect(project_id)
        try:
            progress_percent = max(0, min(100, int(request.POST.get('progress_percent', item.progress_percent))))
        except (TypeError, ValueError):
            progress_percent = item.progress_percent

        if catalog_item:
            item.category = catalog_item['category']
        item.item_name = requested_item_name
        item.target_value = _normalize_metric_target(
            request.POST.get('target_value', item.target_value),
            catalog_item['unit'] if catalog_item else '',
        )[:100]
        item.current_value = request.POST.get('current_value', item.current_value).strip()[:100]
        item.assessment_method = request.POST.get(
            'assessment_method',
            catalog_item['assessment_method'] if catalog_item else item.assessment_method,
        ).strip()[:255]
        item.notes = request.POST.get('notes', item.notes).strip()
        item.status = status
        item.progress_percent = progress_percent

        if item.status == 'completed' or item.progress_percent == 100:
            item.status = 'completed'
            item.progress_percent = 100
            if not item.actual_completion_date:
                item.actual_completion_date = timezone.localdate()
        elif item.status == 'pending' and item.progress_percent > 0:
            item.status = 'in_progress'
        item.save()

        message = f'已更新指标“{item.item_name}”的完成情况。'
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': True, 'message': message})
        messages.success(request, message)
    return _metrics_redirect(project_id)


@require_POST
def create_metrics_item_view(request, project_id):
    project = get_object_or_404(Project, project_id=project_id)
    item_name = request.POST.get('item_name', '').strip()
    catalog_item = MetricsItem.get_catalog_item(item_name)
    if not catalog_item:
        messages.error(request, '请选择指标清单中的具体指标。')
        return _metrics_redirect(project_id)
    target_value = _normalize_metric_target(request.POST.get('target_value'), catalog_item['unit'])
    if not target_value:
        messages.error(request, '请填写目标数量。')
        return _metrics_redirect(project_id)
    analysis, _ = ProjectAnalysis.objects.get_or_create(
        project=project,
        analysis_type='output_metrics',
        defaults={
            'file_name': '手动录入',
            'analysis_result': '手动维护的任务书指标',
            'structured_data': {},
        },
    )
    last_order = analysis.metrics_items.order_by('-sort_order').values_list('sort_order', flat=True).first() or 0
    MetricsItem.objects.create(
        analysis=analysis,
        category=catalog_item['category'],
        item_name=item_name[:255],
        target_value=target_value[:100],
        assessment_method=(
            request.POST.get('assessment_method', '').strip()
            or catalog_item['assessment_method']
        )[:255],
        notes=request.POST.get('notes', '').strip(),
        sort_order=last_order + 1,
        extraction_source='manual',
    )
    messages.success(request, f'已新增指标“{item_name}”。')
    return _metrics_redirect(project_id)


@require_POST
def delete_metrics_item_view(request, project_id, item_id):
    item = get_object_or_404(MetricsItem, id=item_id, analysis__project__project_id=project_id)
    item_name = item.item_name
    item.delete()
    messages.success(request, f'已删除指标“{item_name}”及其佐证关联记录（课题文件本身未删除）。')
    return _metrics_redirect(project_id)


@require_POST
def add_metric_evidence_view(request, project_id, item_id):
    item = get_object_or_404(MetricsItem, id=item_id, analysis__project__project_id=project_id)
    project = item.analysis.project
    relative_path = request.POST.get('relative_path', '').strip().replace('\\', '/')
    target_path = os.path.normpath(_build_abs_path(project.directory_path, relative_path))
    if not relative_path or not _is_within_root(target_path, project.directory_path) or not os.path.isfile(target_path):
        return JsonResponse({'success': False, 'message': '请选择课题文件管理中真实存在的文件。'}, status=400)
    evidence, created = MetricEvidence.objects.get_or_create(
        metric=item,
        relative_path=relative_path,
        defaults={
            'display_name': os.path.basename(target_path)[:255],
            'note': request.POST.get('note', '').strip()[:255],
            'created_by': request.user,
        },
    )
    if not created:
        evidence.note = request.POST.get('note', evidence.note).strip()[:255]
        evidence.display_name = os.path.basename(target_path)[:255]
        evidence.save(update_fields=['note', 'display_name'])
    return JsonResponse({
        'success': True,
        'message': '佐证文件已关联。',
        'evidence': {
            'id': evidence.id,
            'display_name': evidence.display_name,
            'relative_path': evidence.relative_path,
        },
    })


@require_POST
def delete_metric_evidence_view(request, project_id, item_id, evidence_id):
    evidence = get_object_or_404(
        MetricEvidence,
        id=evidence_id,
        metric_id=item_id,
        metric__analysis__project__project_id=project_id,
    )
    evidence.delete()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True, 'message': '已取消佐证文件关联；课题文件未删除。'})
    messages.success(request, '已取消佐证文件关联；课题文件未删除。')
    return _metrics_redirect(project_id)


def funding_category_from_value(value):
    """接受经费管理类别的代码或中文显示值（导出文件即使用显示值）。"""
    text = force_str(value if value is not None else '').strip()
    if not text:
        return None
    choices = dict(Project.FUNDING_CATEGORY_CHOICES)
    if text in choices:
        return text
    for code, label in Project.FUNDING_CATEGORY_CHOICES:
        if text == label:
            return code
    return None


def import_from_excel_view(request):
    category_hint = _valid_funding_category(request.POST.get('funding_category') or request.GET.get('funding_category'))
    if request.method == 'POST':
        excel_file = request.FILES.get('excel_file')
        if not excel_file:
            messages.error(request, "请选择要上传的Excel文件。")
            return redirect('self_funded_project_list' if category_hint == 'self_funded' else 'project_list')

        try:
            workbook = openpyxl.load_workbook(excel_file)
            sheet = workbook.active
            budget_fields = budget_header_map()
            header = [cell.value for cell in sheet[1]]
            # 规范名取自 Project.verbose_name，历史总表的全称写法走别名表。
            field_mapping = {
                '序号': None,  # 跳过序号列
                '课题编号': 'project_id', '课题名称': 'name', '课题归属': 'ownership',
                '归口单位': 'managing_unit', '课题级别': 'level', '课题类型': 'project_type',
                '经费管理类别': 'funding_category',
                '参与角色': 'role', '开始年份': 'start_year', '课题状态': 'status',
                '课题联系人': 'contact_person', '课题负责人': 'project_lead', '开始日期': 'start_date',
                '计划结束日期': 'planned_end_date', '延期时间': 'extension_date', '实际结题时间': 'actual_completion_date',
                '主要研究内容': 'research_content', '备注': 'remarks',
            }
            # 表头里出现过的经费列（含历史名称）动态并入映射。
            for raw_header in header:
                key = normalize_excel_header(raw_header)
                if key and key not in field_mapping and key in budget_fields:
                    field_mapping[key] = budget_fields[key]

            processed_count = 0
            created_count = 0
            updated_count = 0
            skipped_count = 0
            auto_filled_count = 0
            for row in sheet.iter_rows(min_row=2, values_only=True):
                row_data = dict(zip((normalize_excel_header(cell) for cell in header), row))
                model_data = {}
                for header_name, model_field in field_mapping.items():
                    if model_field is None:  # 跳过不需要的字段（如序号）
                        continue
                    source_key = normalize_excel_header(header_name)
                    if source_key not in row_data or row_data[source_key] is None:
                        continue
                    value = row_data[source_key]

                    # 处理日期字段
                    if model_field in ['start_date', 'planned_end_date', 'extension_date', 'actual_completion_date']:
                        if isinstance(value, str) and value.strip():
                            for date_format in ['%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d', '%Y年%m月%d日']:
                                try:
                                    model_data[model_field] = datetime.strptime(value.strip(), date_format).date()
                                    break
                                except ValueError:
                                    continue
                        elif hasattr(value, 'date'):  # Excel日期对象
                            try:
                                model_data[model_field] = value.date()
                            except Exception:
                                continue

                    # 归并历史状态，并拒绝五项之外的状态
                    elif model_field == 'status':
                        normalized_status = Project.normalize_status(value)
                        if not normalized_status:
                            continue
                        model_data[model_field] = normalized_status

                    # 经费管理类别同时接受代码与中文显示值
                    elif model_field == 'funding_category':
                        category_value = funding_category_from_value(value)
                        if category_value:
                            model_data[model_field] = category_value

                    # 处理预算字段
                    elif model_field in BUDGET_FIELDS:
                        if isinstance(value, (int, float, Decimal)) and value > 0:
                            model_data[model_field] = value
                        elif isinstance(value, str) and value.strip():
                            # 移除可能的货币符号和单位
                            clean_value = re.sub(r'[，,]|单位[：:]?|万元|万|元', '', value).strip()
                            try:
                                if clean_value:
                                    model_data[model_field] = Decimal(clean_value)
                            except InvalidOperation:
                                continue

                    # 处理开始年份
                    elif model_field == 'start_year':
                        if isinstance(value, (int, float)):
                            try:
                                model_data[model_field] = int(value)
                            except (ValueError, TypeError):
                                continue
                        elif isinstance(value, str) and value.strip():
                            match = re.search(r'\d{4}', value.strip())
                            if match:
                                model_data[model_field] = int(match.group())

                    # 处理其他字段
                    else:
                        model_data[model_field] = value.strip() if isinstance(value, str) else value

                project_id = model_data.get('project_id')
                if not project_id:
                    skipped_count += 1
                    continue

                if model_data.get('funding_category') not in {'special', 'self_funded'}:
                    model_data['funding_category'] = (
                        'self_funded' if model_data.get('project_type') == '全自筹课题' else category_hint
                    )
                elif model_data.get('project_type') == '全自筹课题':
                    model_data['funding_category'] = 'self_funded'

                existing_project = Project.objects.filter(project_id=project_id).first()
                is_new_project = existing_project is None

                if is_new_project:
                    missing_fields = []
                    required_fields = {
                        'name': '课题名称',
                        'ownership': '课题归属',
                        'level': '课题级别',
                        'project_type': '课题类型',
                        'role': '参与角色',
                        'start_year': '开始年份',
                        'status': '课题状态',
                    }

                    for field, label in required_fields.items():
                        value = model_data.get(field)
                        is_missing = False
                        if field == 'start_year':
                            if value in (None, '') or (isinstance(value, (int, float)) and int(value) == 0):
                                is_missing = True
                        else:
                            if value is None or (isinstance(value, str) and not value.strip()):
                                is_missing = True

                        if is_missing:
                            missing_fields.append(label)
                            if field == 'name':
                                model_data[field] = f"待补全-{project_id}"
                            elif field == 'start_year':
                                model_data[field] = timezone.now().year
                            else:
                                model_data[field] = '待补全'

                    if missing_fields:
                        auto_note = f"【导入待补全】缺失字段：{'、'.join(missing_fields)}"
                        existing_remarks = model_data.get('remarks', '')
                        if existing_remarks and isinstance(existing_remarks, str):
                            model_data['remarks'] = f"{existing_remarks}\n{auto_note}"
                        else:
                            model_data['remarks'] = auto_note
                        auto_filled_count += 1

                    if 'directory_path' not in model_data:
                        model_data['directory_path'] = ''

                print(f"准备创建/更新项目: {project_id}, 数据: {model_data}")
                project, created = Project.objects.update_or_create(
                    project_id=project_id,
                    defaults=model_data
                )
                create_project_directory_structure(project)
                processed_count += 1
                if created:
                    created_count += 1
                else:
                    updated_count += 1
                print(f"{'创建' if created else '更新'}项目: {project_id}")

            print(f"导入完成，共处理 {processed_count} 个项目")
            messages.success(
                request,
                f"数据导入成功！共处理 {processed_count} 个项目（新建 {created_count}，更新 {updated_count}，跳过 {skipped_count}）。"
            )
            if auto_filled_count:
                messages.warning(
                    request,
                    f"其中 {auto_filled_count} 个新项目存在字段缺失，已用“待补全”自动填充，请在详情页补全。"
                )
        except Exception as e:
            import traceback
            error_msg = f"处理文件时出错: {e}\n{traceback.format_exc()}"
            print(error_msg)  # 输出到终端
            messages.error(request, f"处理文件时出错: {e}")

        return redirect('self_funded_project_list' if category_hint == 'self_funded' else 'project_list')
    
    return redirect('self_funded_project_list' if category_hint == 'self_funded' else 'project_list')

def query_assistant_view(request):
    history = []
    if request.method == 'POST':
        try:
            payload = json.loads(request.body or b'{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        question = str(payload.get('question') or '').strip()
        history = payload.get('history') if isinstance(payload.get('history'), list) else []
        service_name = str(payload.get('service_name') or '').strip()
        funding_category = str(payload.get('funding_category') or '').strip()
    else:
        question = (request.GET.get('q') or '').strip()
        service_name = (request.GET.get('service_name') or '').strip()
        funding_category = (request.GET.get('funding_category') or '').strip()
    result = answer_project_question(
        question,
        history=history,
        service_name=service_name or None,
        funding_category=funding_category or None,
    )
    message_html = render_to_string(
        'core/partials/query_assistant_message.html',
        {'result': result},
        request=request,
    )
    return JsonResponse({
        'ok': result.get('ok', False),
        'html': message_html,
        'history_content': result.get('history_content') or result.get('answer') or result.get('error', ''),
        'source': result.get('source', ''),
    })


def self_funded_project_list_view(request):
    return project_list_view(request, funding_category='self_funded')


def self_funded_progress_monitor_view(request):
    return progress_monitor_view(request, funding_category='self_funded')


def self_funded_expense_monitor_view(request):
    return expense_monitor_view(request, funding_category='self_funded')


def statistics_view(request):
    from django.db.models import Sum, Avg
    from django.db.models import Case, When, IntegerField

    base_queryset = Project.objects.all()
    filtered_queryset = _apply_project_filters(base_queryset, request)

    status_distribution = list(filtered_queryset.values('status').annotate(count=Count('status')).order_by('-count'))
    level_distribution = list(filtered_queryset.values('level').annotate(count=Count('level')).order_by('-count'))
    yearly_distribution = list(filtered_queryset.values('start_year').annotate(count=Count('start_year')).order_by('start_year'))
    ownership_distribution = list(filtered_queryset.values('ownership').annotate(count=Count('ownership')).order_by('-count'))
    type_distribution = list(filtered_queryset.values('project_type').annotate(count=Count('project_type')).order_by('-count'))
    role_distribution = list(filtered_queryset.values('role').annotate(count=Count('role')).order_by('-count'))

    metrics_queryset = MetricsItem.objects.filter(analysis__project__in=filtered_queryset)
    metrics_category_distribution = list(metrics_queryset.values('category').annotate(count=Count('category')).order_by('-count'))
    metrics_status_distribution = list(metrics_queryset.values('status').annotate(count=Count('status')).order_by('-count'))

    status_count_dict = {item['status']: item['count'] for item in metrics_status_distribution}
    total_metrics = sum(status_count_dict.values())
    completed_metrics = status_count_dict.get('completed', 0)
    in_progress_metrics = status_count_dict.get('in_progress', 0)
    pending_metrics = status_count_dict.get('pending', 0)

    category_stats = metrics_queryset.values('category').annotate(
        total=Count('id'),
        completed=Count(Case(When(status='completed', then=1), output_field=IntegerField()))
    )
    category_stats_dict = {item['category']: item for item in category_stats}

    metrics_completion_by_category = []
    for category_code, category_name in MetricsItem.CATEGORY_CHOICES:
        stats = category_stats_dict.get(category_code, {'total': 0, 'completed': 0})
        category_total = stats['total']
        category_completed = stats['completed']
        completion_rate = (category_completed / category_total * 100) if category_total > 0 else 0

        metrics_completion_by_category.append({
            'category': category_name,
            'total': category_total,
            'completed': category_completed,
            'completion_rate': completion_rate
        })

    budget_stats = filtered_queryset.aggregate(
        total_budget_sum=Sum('total_budget'),
        external_funding_sum=Sum('external_funding'),
        institute_funding_sum=Sum('institute_funding'),
        unit_funding_sum=Sum('unit_funding'),
        avg_total_budget=Avg('total_budget')
    )

    yearly_budget = list(filtered_queryset.values('start_year').annotate(
        total_budget_sum=Sum('total_budget'),
        count=Count('project_id')
    ).order_by('start_year'))

    status_budget = list(filtered_queryset.values('status').annotate(
        total_budget_sum=Sum('total_budget'),
        count=Count('project_id')
    ).order_by('-total_budget_sum'))

    level_budget = list(filtered_queryset.values('level').annotate(
        total_budget_sum=Sum('total_budget'),
        count=Count('project_id')
    ).order_by('-total_budget_sum'))

    total_projects = filtered_queryset.count()
    completed_projects = filtered_queryset.filter(status__in=['结题', '终止']).count()
    ongoing_projects = total_projects - completed_projects

    distinct_years = base_queryset.values_list('start_year', flat=True).distinct().order_by('-start_year')
    distinct_statuses = base_queryset.values_list('status', flat=True).distinct().order_by('status')
    distinct_levels = base_queryset.values_list('level', flat=True).distinct().order_by('level')
    distinct_ownerships = base_queryset.values_list('ownership', flat=True).distinct().order_by('ownership')
    distinct_types = base_queryset.values_list('project_type', flat=True).distinct().order_by('project_type')
    distinct_roles = base_queryset.values_list('role', flat=True).distinct().order_by('role')
    distinct_units = base_queryset.exclude(managing_unit__isnull=True).exclude(managing_unit__exact='').values_list('managing_unit', flat=True).distinct().order_by('managing_unit')
    distinct_leads = base_queryset.exclude(project_lead__isnull=True).exclude(project_lead__exact='').values_list('project_lead', flat=True).distinct().order_by('project_lead')

    selected_years = _extract_list_param(request, 'year')
    selected_statuses = _extract_list_param(request, 'status')
    selected_levels = _extract_list_param(request, 'level')
    selected_ownerships = _extract_list_param(request, 'ownership')
    selected_types = _extract_list_param(request, 'project_type')
    selected_roles = _extract_list_param(request, 'role')
    selected_units = _extract_list_param(request, 'managing_unit')
    selected_leads = _extract_list_param(request, 'project_lead')
    min_budget = request.GET.get('min_budget', '').strip()
    max_budget = request.GET.get('max_budget', '').strip()
    start_date_from = request.GET.get('start_date_from', '').strip()
    start_date_to = request.GET.get('start_date_to', '').strip()
    end_date_from = request.GET.get('end_date_from', '').strip()
    end_date_to = request.GET.get('end_date_to', '').strip()
    query = request.GET.get('q', '').strip()
    filters_applied = any([
        query, selected_years, selected_statuses, selected_levels, selected_ownerships,
        selected_types, selected_roles, selected_units, selected_leads,
        min_budget, max_budget, start_date_from, start_date_to, end_date_from, end_date_to
    ])

    context = {
        'status_data': json.dumps({
            'labels': [item['status'] for item in status_distribution],
            'data': [item['count'] for item in status_distribution],
        }),
        'level_data': json.dumps({
            'labels': [item['level'] for item in level_distribution],
            'data': [item['count'] for item in level_distribution],
        }),
        'yearly_data': json.dumps({
            'labels': [str(item['start_year']) for item in yearly_distribution],
            'data': [item['count'] for item in yearly_distribution],
        }),
        'ownership_data': json.dumps({
            'labels': [item['ownership'] for item in ownership_distribution],
            'data': [item['count'] for item in ownership_distribution],
        }),
        'type_data': json.dumps({
            'labels': [item['project_type'] for item in type_distribution],
            'data': [item['count'] for item in type_distribution],
        }),
        'role_data': json.dumps({
            'labels': [item['role'] for item in role_distribution],
            'data': [item['count'] for item in role_distribution],
        }),
        'yearly_budget_data': json.dumps({
            'labels': [str(item['start_year']) for item in yearly_budget],
            'data': [float(item['total_budget_sum'] or 0) for item in yearly_budget],
        }),
        'status_budget_data': json.dumps({
            'labels': [item['status'] for item in status_budget],
            'data': [float(item['total_budget_sum'] or 0) for item in status_budget],
        }),
        'level_budget_data': json.dumps({
            'labels': [item['level'] for item in level_budget],
            'data': [float(item['total_budget_sum'] or 0) for item in level_budget],
        }),
        'budget_stats': budget_stats,
        'total_projects': total_projects,
        'ongoing_projects': ongoing_projects,
        'completed_projects': completed_projects,
        'metrics_category_data': json.dumps({
            'labels': [MetricsItem.get_category_label_map().get(item['category'], item['category']) for item in metrics_category_distribution],
            'data': [item['count'] for item in metrics_category_distribution],
        }),
        'metrics_status_data': json.dumps({
            'labels': [dict(MetricsItem.STATUS_CHOICES).get(item['status'], item['status']) for item in metrics_status_distribution],
            'data': [item['count'] for item in metrics_status_distribution],
        }),
        'metrics_completion_data': json.dumps({
            'labels': [item['category'] for item in metrics_completion_by_category],
            'completion_rates': [item['completion_rate'] for item in metrics_completion_by_category],
            'totals': [item['total'] for item in metrics_completion_by_category],
            'completed': [item['completed'] for item in metrics_completion_by_category],
        }),
        'total_metrics': total_metrics,
        'completed_metrics': completed_metrics,
        'in_progress_metrics': in_progress_metrics,
        'pending_metrics': pending_metrics,
        'metrics_completion_by_category': metrics_completion_by_category,
        'distinct_years': distinct_years,
        'distinct_statuses': distinct_statuses,
        'distinct_levels': distinct_levels,
        'distinct_ownerships': distinct_ownerships,
        'distinct_types': distinct_types,
        'distinct_roles': distinct_roles,
        'distinct_units': distinct_units,
        'distinct_leads': distinct_leads,
        'selected_years': selected_years,
        'selected_statuses': selected_statuses,
        'selected_levels': selected_levels,
        'selected_ownerships': selected_ownerships,
        'selected_types': selected_types,
        'selected_roles': selected_roles,
        'selected_units': selected_units,
        'selected_leads': selected_leads,
        'min_budget': min_budget,
        'max_budget': max_budget,
        'start_date_from': start_date_from,
        'start_date_to': start_date_to,
        'end_date_from': end_date_from,
        'end_date_to': end_date_to,
        'query': query,
        'filters_applied': filters_applied,
    }
    return render(request, 'core/statistics.html', context)

def api_config_view(request):
    """API配置管理页面"""
    
    # 获取现有配置
    configs = APIConfig.objects.all().order_by('service_name')
    
    if request.method == 'POST':
        service_name = request.POST.get('service_name')
        api_key = request.POST.get('api_key', '').strip()
        base_url = request.POST.get('base_url', '').strip()
        model_name = request.POST.get('model_name', '').strip()
        action = request.POST.get('action')

        allowed_services = dict(APIConfig.SERVICE_CHOICES)
        if service_name not in allowed_services:
            messages.error(request, '请选择有效的AI服务。')
            return redirect('api_config')

        if action == 'save' and service_name:
            try:
                defaults = provider_defaults(service_name)
                if service_name != 'local' and not api_key:
                    existing = APIConfig.objects.filter(service_name=service_name).first()
                    if not existing or not existing.get_api_key().strip():
                        raise ValidationError('该云端服务必须填写API密钥。')
                base_url = base_url or defaults.get('base_url', '')
                model_name = model_name or defaults.get('model', '')
                if not base_url or not model_name:
                    raise ValidationError('服务地址和模型名称不能为空。')
                # 创建或更新配置
                config, created = APIConfig.objects.update_or_create(
                    service_name=service_name,
                    defaults={
                        'base_url': base_url,
                        'model_name': model_name,
                        'is_active': True,
                        'test_success': False,
                        'last_test_time': None
                    }
                )

                # 留空表示保留云端已有密钥；本地服务允许不配置密钥。
                if api_key or service_name == 'local':
                    config.set_api_key(api_key)
                config.full_clean()
                config.save()
                
                action_text = '创建' if created else '更新'
                messages.success(request, f'{config.get_service_name_display()} API配置{action_text}成功！')
                
            except ValidationError as e:
                messages.error(request, f'保存API配置失败: {" ".join(e.messages)}')
            except Exception as e:
                messages.error(request, f'保存API配置失败: {e}')
        
        elif action == 'test' and service_name:
            try:
                config = APIConfig.objects.get(service_name=service_name)
                test_success, detail = test_api_connection(config)

                # 更新测试结果
                config.test_success = test_success
                config.last_test_time = timezone.now()
                config.save(update_fields=['test_success', 'last_test_time', 'updated_at'])

                if test_success:
                    messages.success(request, f'{config.get_service_name_display()} 模型连接测试成功！{detail}')
                else:
                    messages.error(request, f'{config.get_service_name_display()} 模型连接测试失败：{detail}')
                        
            except APIConfig.DoesNotExist:
                messages.error(request, '请先保存API配置再进行测试')
            except Exception as e:
                messages.error(request, f'API测试失败: {e}')
        
        elif action == 'toggle' and service_name:
            try:
                config = APIConfig.objects.get(service_name=service_name)
                config.is_active = not config.is_active
                config.save()
                
                status_text = '启用' if config.is_active else '禁用'
                messages.info(request, f'{config.get_service_name_display()} 服务已{status_text}')
                
            except APIConfig.DoesNotExist:
                messages.error(request, 'API配置不存在')
        
        elif action == 'delete' and service_name:
            try:
                config = APIConfig.objects.get(service_name=service_name)
                service_display = config.get_service_name_display()
                config.delete()
                messages.info(request, f'{service_display} API配置已删除')
                
            except APIConfig.DoesNotExist:
                messages.error(request, 'API配置不存在')
        
        return redirect('api_config')
    
    # 刷新配置列表
    configs = APIConfig.objects.all().order_by('service_name')
    
    context = {
        'configs': configs,
        'service_choices': APIConfig.SERVICE_CHOICES,
        'service_defaults_json': json.dumps({
            name: provider_defaults(name)
            for name, _label in APIConfig.SERVICE_CHOICES
        }, ensure_ascii=False),
    }
    return render(request, 'core/api_config.html', context)

def test_api_connection(config):
    """用一次最小对话测试任意 OpenAI 兼容模型配置。"""
    try:
        if not config_is_usable(config):
            return False, '配置不完整，请检查服务地址、模型名称和API密钥。'
        payload = provider_payload(
            config,
            messages=[{'role': 'user', 'content': '只回复：连接成功'}],
            max_tokens=16,
            temperature=0,
            stream=False,
        )
        result = post_chat_completion(config, payload, timeout=20)
        content = str(result['choices'][0]['message'].get('content') or '').strip()
        if not content:
            return False, '模型返回了空内容。'
        return True, f' 当前模型：{get_model_name(config)}。'
    except Exception as e:
        return False, str(e)[:300]

def init_system_view(request):
    """系统初始化视图"""
    from django.core.management import execute_from_command_line
    from django.db import connection
    import sys
    
    init_results = []
    
    try:
        # 1. 检查数据库连接
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        init_results.append({"step": "数据库连接", "status": "成功", "message": "数据库连接正常"})
    except Exception as e:
        init_results.append({"step": "数据库连接", "status": "失败", "message": f"数据库连接失败: {e}"})
    
    try:
        # 2. 确保projects目录存在
        projects_root = str(settings.PROJECTS_ROOT)
        os.makedirs(projects_root, exist_ok=True)
        init_results.append({"step": "项目目录", "status": "成功", "message": f"项目根目录已创建: {projects_root}"})
    except Exception as e:
        init_results.append({"step": "项目目录", "status": "失败", "message": f"创建项目目录失败: {e}"})
    
    try:
        # 3. 检查并创建所有项目的目录结构
        projects = Project.objects.all()
        created_count = 0
        for project in projects:
            if not project.directory_path or not os.path.exists(project.directory_path):
                create_project_directory_structure(project)
                created_count += 1
        
        if created_count > 0:
            init_results.append({"step": "项目目录结构", "status": "成功", "message": f"已为 {created_count} 个项目创建目录结构"})
        else:
            init_results.append({"step": "项目目录结构", "status": "成功", "message": "所有项目目录结构已存在"})
    except Exception as e:
        init_results.append({"step": "项目目录结构", "status": "失败", "message": f"创建项目目录结构失败: {e}"})
    
    try:
        # 4. 检查静态文件
        static_root = os.path.join(settings.BASE_DIR, 'staticfiles')
        if os.path.exists(static_root):
            init_results.append({"step": "静态文件", "status": "成功", "message": "静态文件目录存在"})
        else:
            init_results.append({"step": "静态文件", "status": "警告", "message": "静态文件目录不存在，请运行 collectstatic"})
    except Exception as e:
        init_results.append({"step": "静态文件", "status": "失败", "message": f"检查静态文件失败: {e}"})
    
    # 5. 系统信息
    try:
        total_projects = Project.objects.count()
        init_results.append({"step": "系统状态", "status": "信息", "message": f"当前系统中共有 {total_projects} 个项目"})
    except Exception as e:
        init_results.append({"step": "系统状态", "status": "失败", "message": f"获取系统状态失败: {e}"})
    
    context = {
        'init_results': init_results,
        'success_count': len([r for r in init_results if r['status'] == '成功']),
        'error_count': len([r for r in init_results if r['status'] == '失败']),
        'warning_count': len([r for r in init_results if r['status'] == '警告']),
    }
    
    return render(request, 'core/init_system.html', context)

def test_upload_view(request):
    """测试文件上传页面"""
    return render(request, 'core/test_upload.html')

def get_network_config_api(request):
    """API: 获取网络共享配置"""
    config = get_network_config()
    return JsonResponse(config)

def get_network_config():
    """获取网络共享配置"""
    config_file = Path(settings.BASE_DIR) / 'network_config.json'
    default_config = {
        'network_share_path': '',  # 网络共享路径，如 \\192.168.1.100\projects
        'enable_network_share': False,  # 是否启用网络共享路径
        'enable_web_file_trial': True,  # 是否启用 Web 文件管理试用入口
        'readonly_can_download': False,  # 是否允许只读用户下载课题文件
    }
    
    if config_file.exists():
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
                # 合并默认配置，确保新字段存在
                return {**default_config, **config}
        except Exception:
            pass
    return default_config

def save_network_config(config):
    """保存网络共享配置"""
    config_file = Path(settings.BASE_DIR) / 'network_config.json'
    with open(config_file, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def user_management_view(request):
    """简单账号管理：管理员或只读用户。"""
    if not is_system_admin(request.user):
        return HttpResponseForbidden('仅管理员可以管理用户。')

    User = get_user_model()

    if request.method == 'POST':
        action = request.POST.get('action', '').strip()

        if action == 'create':
            username = request.POST.get('username', '').strip()
            display_name = request.POST.get('display_name', '').strip()
            role = request.POST.get('role', 'readonly')
            password1 = request.POST.get('password1', '')
            password2 = request.POST.get('password2', '')

            if not username:
                messages.error(request, '请输入用户名。')
            elif User.objects.filter(username=username).exists():
                messages.error(request, '该用户名已存在。')
            elif password1 != password2:
                messages.error(request, '两次输入的密码不一致。')
            else:
                candidate = User(username=username, first_name=display_name)
                try:
                    validate_password(password1, user=candidate)
                    candidate.is_staff = role == 'admin'
                    candidate.is_active = True
                    candidate.set_password(password1)
                    candidate.save()
                    messages.success(request, f'用户 {username} 创建成功。')
                except ValidationError as exc:
                    messages.error(request, ' '.join(exc.messages))

        elif action == 'update':
            target = get_object_or_404(User, pk=request.POST.get('user_id'))
            username = request.POST.get('username', '').strip()
            display_name = request.POST.get('display_name', '').strip()
            role = request.POST.get('role', 'readonly')
            is_active = request.POST.get('is_active') == 'on'
            wants_admin = role == 'admin' or target.is_superuser
            admin_count = User.objects.filter(is_active=True).filter(Q(is_staff=True) | Q(is_superuser=True)).count()

            if not username:
                messages.error(request, '用户名不能为空。')
            elif User.objects.exclude(pk=target.pk).filter(username=username).exists():
                messages.error(request, '该用户名已被其他账号使用。')
            elif target.pk == request.user.pk and (not is_active or not wants_admin):
                messages.error(request, '不能停用或降级当前登录的管理员账号。')
            elif is_system_admin(target) and target.is_active and (not is_active or not wants_admin) and admin_count <= 1:
                messages.error(request, '系统至少需要保留一个启用的管理员账号。')
            else:
                target.username = username
                target.first_name = display_name
                target.is_staff = wants_admin
                target.is_active = is_active
                target.save(update_fields=['username', 'first_name', 'is_staff', 'is_active'])
                messages.success(request, f'用户 {username} 已更新。')

        elif action == 'reset_password':
            target = get_object_or_404(User, pk=request.POST.get('user_id'))
            password1 = request.POST.get('password1', '')
            password2 = request.POST.get('password2', '')
            if password1 != password2:
                messages.error(request, '两次输入的密码不一致。')
            else:
                try:
                    validate_password(password1, user=target)
                    target.set_password(password1)
                    target.save(update_fields=['password'])
                    if target.pk == request.user.pk:
                        update_session_auth_hash(request, target)
                    messages.success(request, f'用户 {target.username} 的密码已重置。')
                except ValidationError as exc:
                    messages.error(request, ' '.join(exc.messages))
        else:
            messages.error(request, '无效的用户管理操作。')

        return redirect('user_management')

    users = User.objects.all().order_by('-is_superuser', '-is_staff', 'username')
    operation_logs = OperationLog.objects.select_related('user').all()[:100]
    return render(request, 'core/user_management.html', {
        'users': users,
        'operation_logs': operation_logs,
    })

def _settings_sort_order(raw_value):
    try:
        return max(0, min(9999, int(str(raw_value or '0').strip())))
    except (TypeError, ValueError):
        raise ValidationError('排序必须填写 0 到 9999 之间的整数。')


def _metric_settings_redirect():
    return redirect(f'{reverse("settings")}#metric-settings')


def settings_view(request):
    """系统设置页面"""
    network_config = get_network_config()
    
    if request.method == 'POST':
        action = request.POST.get('action', 'save_path')
        
        if action == 'create_metric_category':
            category_name = request.POST.get('category_name', '').strip()
            if not category_name:
                messages.error(request, '指标大类名称不能为空。')
            elif len(category_name) > 100:
                messages.error(request, '指标大类名称不能超过 100 个字符。')
            elif MetricsCategory.objects.filter(name=category_name).exists():
                messages.error(request, '该指标大类已存在。')
            else:
                try:
                    MetricsCategory.objects.create(
                        code=f'custom_{uuid.uuid4().hex[:12]}',
                        name=category_name,
                        sort_order=_settings_sort_order(request.POST.get('sort_order')),
                        is_active=request.POST.get('is_active') == 'on',
                    )
                    messages.success(request, f'已新增指标大类“{category_name}”。')
                except ValidationError as exc:
                    messages.error(request, ' '.join(exc.messages))
            return _metric_settings_redirect()
        elif action in {'update_metric_category', 'delete_metric_category'}:
            category = get_object_or_404(MetricsCategory, pk=request.POST.get('category_id'))
            if action == 'delete_metric_category':
                if category.indicators.exists():
                    messages.error(request, '该大类下仍有具体指标，请先删除或转移具体指标。')
                elif MetricsItem.objects.filter(category=category.code).exists():
                    messages.error(request, '已有课题使用该大类，不能删除；可以改为停用。')
                else:
                    category_name = category.name
                    category.delete()
                    messages.success(request, f'已删除指标大类“{category_name}”。')
                return _metric_settings_redirect()

            category_name = request.POST.get('category_name', '').strip()
            if not category_name:
                messages.error(request, '指标大类名称不能为空。')
            elif len(category_name) > 100:
                messages.error(request, '指标大类名称不能超过 100 个字符。')
            elif MetricsCategory.objects.exclude(pk=category.pk).filter(name=category_name).exists():
                messages.error(request, '该指标大类名称已被使用。')
            else:
                try:
                    category.name = category_name
                    category.sort_order = _settings_sort_order(request.POST.get('sort_order'))
                    category.is_active = request.POST.get('is_active') == 'on'
                    category.save(update_fields=['name', 'sort_order', 'is_active', 'updated_at'])
                    messages.success(request, f'已更新指标大类“{category_name}”。')
                except ValidationError as exc:
                    messages.error(request, ' '.join(exc.messages))
            return _metric_settings_redirect()
        elif action == 'create_metric_indicator':
            category = get_object_or_404(MetricsCategory, pk=request.POST.get('category_id'))
            indicator_name = request.POST.get('indicator_name', '').strip()
            if not indicator_name:
                messages.error(request, '具体指标名称不能为空。')
            elif len(indicator_name) > 255:
                messages.error(request, '具体指标名称不能超过 255 个字符。')
            elif MetricIndicatorDefinition.objects.filter(name=indicator_name).exists():
                messages.error(request, '该具体指标已存在。')
            else:
                try:
                    MetricIndicatorDefinition.objects.create(
                        category=category,
                        name=indicator_name,
                        unit=request.POST.get('unit', '').strip()[:20],
                        assessment_method=request.POST.get('assessment_method', '').strip()[:255],
                        sort_order=_settings_sort_order(request.POST.get('sort_order')),
                        is_active=request.POST.get('is_active') == 'on',
                    )
                    messages.success(request, f'已新增具体指标“{indicator_name}”。')
                except ValidationError as exc:
                    messages.error(request, ' '.join(exc.messages))
            return _metric_settings_redirect()
        elif action in {'update_metric_indicator', 'delete_metric_indicator'}:
            indicator = get_object_or_404(MetricIndicatorDefinition, pk=request.POST.get('indicator_id'))
            if action == 'delete_metric_indicator':
                if MetricsItem.objects.filter(item_name=indicator.name).exists():
                    messages.error(request, '已有课题使用该具体指标，不能删除；可以改为停用。')
                else:
                    indicator_name = indicator.name
                    indicator.delete()
                    messages.success(request, f'已删除具体指标“{indicator_name}”。')
                return _metric_settings_redirect()

            indicator_name = request.POST.get('indicator_name', '').strip()
            category = get_object_or_404(MetricsCategory, pk=request.POST.get('category_id'))
            if not indicator_name:
                messages.error(request, '具体指标名称不能为空。')
            elif len(indicator_name) > 255:
                messages.error(request, '具体指标名称不能超过 255 个字符。')
            elif MetricIndicatorDefinition.objects.exclude(pk=indicator.pk).filter(name=indicator_name).exists():
                messages.error(request, '该具体指标名称已被使用。')
            else:
                try:
                    indicator.category = category
                    indicator.name = indicator_name
                    indicator.unit = request.POST.get('unit', '').strip()[:20]
                    indicator.assessment_method = request.POST.get('assessment_method', '').strip()[:255]
                    indicator.sort_order = _settings_sort_order(request.POST.get('sort_order'))
                    indicator.is_active = request.POST.get('is_active') == 'on'
                    indicator.save(update_fields=[
                        'category', 'name', 'unit', 'assessment_method',
                        'sort_order', 'is_active', 'updated_at',
                    ])
                    messages.success(request, f'已更新具体指标“{indicator_name}”。')
                except ValidationError as exc:
                    messages.error(request, ' '.join(exc.messages))
            return _metric_settings_redirect()
        elif action == 'save_backup_schedule':
            backup_interval_days = request.POST.get('backup_interval_days', '').strip()
            backup_time = request.POST.get('backup_time', '').strip()
            backup_enabled = request.POST.get('backup_enabled') == 'on'
            try:
                updated_schedule = backup_scheduler.update_backup_schedule(
                    backup_interval_days,
                    backup_time,
                    backup_enabled,
                )
                enabled_text = '启用' if backup_enabled else '停用'
                messages.success(
                    request,
                    f'自动备份已{enabled_text}；计划时间为每隔 {int(backup_interval_days)} 天的 {backup_time}。',
                )
                if not updated_schedule.get('available'):
                    messages.warning(request, '时间已保存，但暂时无法重新读取计划任务状态，请稍后刷新确认。')
                return redirect('settings')
            except backup_scheduler.BackupScheduleError as exc:
                messages.error(request, str(exc))
        elif action == 'save_readonly_permissions':
            network_config['readonly_can_download'] = request.POST.get('readonly_can_download') == 'on'
            save_network_config(network_config)
            if network_config['readonly_can_download']:
                messages.success(request, '已允许只读用户下载课题文件。')
            else:
                messages.info(request, '已禁止只读用户下载课题文件。')
            return redirect('settings')
        elif action == 'save_network':
            # 保存网络共享配置
            network_share_path = request.POST.get('network_share_path', '').strip()
            enable_network_share = request.POST.get('enable_network_share') == 'on'
            enable_web_file_trial = request.POST.get('enable_web_file_trial') == 'on'
            
            network_config['network_share_path'] = network_share_path
            network_config['enable_network_share'] = enable_network_share
            network_config['enable_web_file_trial'] = enable_web_file_trial
            save_network_config(network_config)
            
            if enable_network_share and network_share_path:
                messages.success(request, f'网络共享路径已配置为: {network_share_path}')
            else:
                messages.info(request, '已禁用网络共享路径，将使用本地路径')
            if enable_web_file_trial:
                messages.info(request, 'Web 文件管理试用入口已启用')
            else:
                messages.info(request, 'Web 文件管理试用入口已关闭')
        elif action == 'save_path':
            # 保存项目路径
            projects_root = request.POST.get('projects_root', '').strip()
            migrate_existing = request.POST.get('migrate_existing') == 'on'
            
            if projects_root:
                # 验证路径格式
                try:
                    path_obj = Path(projects_root)
                    # 检查路径是否为绝对路径
                    if not path_obj.is_absolute():
                        messages.error(request, '请输入绝对路径（完整路径）')
                    else:
                        # 更新settings.py文件
                        settings_file = Path(settings.BASE_DIR) / 'project_manager' / 'settings.py'
                        
                        with open(settings_file, 'r', encoding='utf-8') as f:
                            content = f.read()
                        
                        # 替换PROJECTS_ROOT配置
                        import re
                        pattern = r"PROJECTS_ROOT = .*"
                        if os.name == 'nt':  # Windows
                            replacement = f"PROJECTS_ROOT = Path(r'{projects_root}')"
                        else:  # Linux/Mac
                            replacement = f"PROJECTS_ROOT = Path('{projects_root}')"
                        
                        new_content = re.sub(pattern, replacement, content)
                        
                        with open(settings_file, 'w', encoding='utf-8') as f:
                            f.write(new_content)
                        
                        settings.PROJECTS_ROOT = Path(projects_root)
                        os.makedirs(projects_root, exist_ok=True)
                        
                        moved_count = 0
                        updated_count = 0
                        for project in Project.objects.all():
                            new_folder_name = _get_project_folder_name(project)
                            new_path = os.path.join(projects_root, new_folder_name)
                            
                            if migrate_existing and project.directory_path and os.path.exists(project.directory_path):
                                if _normalize_path(project.directory_path) != _normalize_path(new_path):
                                    moved, _ = _move_path_with_backup(project.directory_path, new_path)
                                    if moved:
                                        moved_count += 1
                            
                            if project.directory_path != new_path:
                                project.directory_path = new_path
                                project.save(update_fields=['directory_path'])
                                updated_count += 1
                        
                        if migrate_existing:
                            messages.success(request, f'项目路径已更新为: {projects_root}。已更新 {updated_count} 个项目目录路径，迁移 {moved_count} 个目录。请重启服务器以使更改生效。')
                        else:
                            messages.success(request, f'项目路径已更新为: {projects_root}。已更新 {updated_count} 个项目目录路径。请重启服务器以使更改生效。')
                        
                except Exception as e:
                    messages.error(request, f'路径格式错误: {str(e)}')
            else:
                messages.error(request, '请输入有效的路径')
        elif action == 'update_project_path':
            project_id = request.POST.get('project_id', '').strip()
            project_path = request.POST.get('project_path', '').strip()
            move_project_files = request.POST.get('move_project_files') == 'on'
            
            if not project_id or not project_path:
                messages.error(request, '请输入项目编号和目标路径')
            else:
                try:
                    path_obj = Path(project_path)
                    if not path_obj.is_absolute():
                        messages.error(request, '请输入绝对路径（完整路径）')
                    else:
                        project = get_object_or_404(Project, project_id=project_id)
                        if move_project_files and project.directory_path and os.path.exists(project.directory_path):
                            _move_path_with_backup(project.directory_path, project_path)
                        os.makedirs(project_path, exist_ok=True)
                        project.directory_path = project_path
                        project.save(update_fields=['directory_path'])
                        messages.success(request, f'项目 {project.project_id} 路径已更新为: {project_path}')
                except Exception as e:
                    messages.error(request, f'更新项目路径失败: {str(e)}')
    
    # 获取当前配置的项目路径
    current_projects_root = str(settings.PROJECTS_ROOT)
    network_config = get_network_config()  # 重新获取最新配置
    projects = Project.objects.all().order_by('project_id')
    metric_categories = MetricsCategory.objects.prefetch_related('indicators').all()
    
    context = {
        'current_projects_root': current_projects_root,
        'network_share_path': network_config.get('network_share_path', ''),
        'enable_network_share': network_config.get('enable_network_share', False),
        'enable_web_file_trial': network_config.get('enable_web_file_trial', True),
        'readonly_can_download': network_config.get('readonly_can_download', False),
        'projects': projects,
        'metric_categories': metric_categories,
    }
    
    return render(request, 'core/settings.html', context)


def backup_schedule_status_view(request):
    """异步读取 Windows 计划任务，避免阻塞系统设置首页。"""
    backup_schedule = backup_scheduler.get_backup_schedule()
    return render(
        request,
        'core/partials/backup_schedule_settings.html',
        {'backup_schedule': backup_schedule},
    )
