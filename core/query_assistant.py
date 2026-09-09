"""面向课题台账的只读自然语言查询助手。"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

import requests
from django.db.models import Q, QuerySet
from django.utils import timezone

from .models import APIConfig, Project


MAX_RESULTS = 100
SUPPORTED_ACTIONS = {'list_projects', 'count_projects', 'compare_project_counts'}
CHOICE_FILTERS = {
    'funding_category': {value for value, _ in Project.FUNDING_CATEGORY_CHOICES},
    'ownership': {value for value, _ in Project.OWNERSHIP_CHOICES},
    'level': {value for value, _ in Project.LEVEL_CHOICES},
    'project_type': {value for value, _ in Project.TYPE_CHOICES},
    'role': {value for value, _ in Project.ROLE_CHOICES},
    'status': {value for value, _ in Project.STATUS_CHOICES},
}
FILTER_LABELS = {
    'funding_category': '经费管理类别',
    'ownership': '课题归属',
    'level': '课题级别',
    'project_type': '课题类型',
    'role': '参与角色',
    'status': '课题状态',
    'start_year': '开始年份',
    'effective_end_year': '有效结题年份',
    'managing_unit': '归口单位',
    'person': '相关人员',
}


def _normalize_year(value: Any) -> int | None:
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    if 0 <= year < 100:
        year += 2000
    return year if 1900 <= year <= 2200 else None


def _extract_years(question: str) -> list[int]:
    years = []
    for raw_year in re.findall(r'(?<!\d)(\d{4}|\d{2})(?!\d)\s*年?', question):
        year = _normalize_year(raw_year)
        if year and year not in years:
            years.append(year)
    return years


def _split_people(value: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r'[,，、;；/\s]+', value or '')
        if 1 < len(item.strip()) <= 30
    ]


def _find_known_person(question: str) -> str:
    people = set()
    for lead, contact in Project.objects.values_list('project_lead', 'contact_person'):
        people.update(_split_people(lead))
        people.update(_split_people(contact))
    return next((person for person in sorted(people, key=len, reverse=True) if person in question), '')


def _extract_person(question: str) -> str:
    known_person = _find_known_person(question)
    if known_person:
        return known_person

    probe = re.sub(
        r'^(?:请|麻烦)?(?:给我)?(?:查一下|查询一下|查询|查找|看看|看一下)',
        '',
        question.strip(),
    )
    patterns = [
        r'(?P<person>[\u4e00-\u9fff·]{2,10})(?:负责|联系|参与|牵头)(?:的|了)?(?:哪些|什么|所有)?课题',
        r'(?P<person>[\u4e00-\u9fff·]{2,10})(?:有|的)(?:哪些|什么|所有)?课题',
    ]
    for pattern in patterns:
        matched = re.search(pattern, probe)
        if matched:
            return matched.group('person')
    return ''


def _extract_common_filters(question: str) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    for field_name, choices in CHOICE_FILTERS.items():
        matched = next((choice for choice in choices if choice in question), None)
        if matched:
            filters[field_name] = matched

    managing_units = Project.objects.exclude(managing_unit='').values_list('managing_unit', flat=True).distinct()
    matched_unit = next(
        (unit for unit in sorted(managing_units, key=len, reverse=True) if unit and unit in question),
        None,
    )
    if matched_unit:
        filters['managing_unit'] = matched_unit
    return filters


def _question_year(question: str, today: date) -> int | None:
    if '今年' in question or '本年' in question:
        return today.year
    if '明年' in question:
        return today.year + 1
    if '去年' in question:
        return today.year - 1
    years = _extract_years(question)
    return years[0] if years else None


def _wants_count(question: str) -> bool:
    return any(keyword in question for keyword in ('多少', '几项', '数量', '总数', '统计'))


def _local_plan(question: str, today: date) -> dict[str, Any] | None:
    filters = _extract_common_filters(question)
    years = _extract_years(question)

    if '比' in question and len(years) >= 2 and '课题' in question:
        return {
            'action': 'compare_project_counts',
            'filters': filters,
            'years': years[:2],
        }

    if '结题' in question:
        if not any(keyword in question for keyword in ('已结题', '结题状态', '状态为结题')):
            filters.pop('status', None)
        end_year = _question_year(question, today)
        if end_year:
            filters['effective_end_year'] = end_year
            filters['exclude_closed'] = any(
                keyword in question for keyword in ('要结题', '待结题', '将结题', '计划结题', '需结题')
            )
            return {
                'action': 'count_projects' if _wants_count(question) else 'list_projects',
                'filters': filters,
            }

    if '课题' in question:
        person = _extract_person(question)
        if person:
            filters['person'] = person
            filters['person_role'] = (
                'lead' if '负责' in question or '负责人' in question
                else 'contact' if '联系' in question or '联系人' in question
                else 'any'
            )

        year = _question_year(question, today)
        if year:
            filters['start_year'] = year

        if filters or _wants_count(question) or '所有课题' in question:
            return {
                'action': 'count_projects' if _wants_count(question) else 'list_projects',
                'filters': filters,
            }
    return None


def _extract_json_object(raw_text: str) -> dict[str, Any] | None:
    raw_text = (raw_text or '').strip()
    try:
        payload = json.loads(raw_text)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        start = raw_text.find('{')
        end = raw_text.rfind('}')
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(raw_text[start:end + 1])
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            return None


def _validate_plan(raw_plan: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(raw_plan, dict) or raw_plan.get('action') not in SUPPORTED_ACTIONS:
        return None

    filters = raw_plan.get('filters') if isinstance(raw_plan.get('filters'), dict) else {}
    clean_filters: dict[str, Any] = {}
    for field_name, allowed_values in CHOICE_FILTERS.items():
        value = str(filters.get(field_name) or '').strip()
        if value in allowed_values:
            clean_filters[field_name] = value

    for field_name, max_length in (('person', 50), ('managing_unit', 100)):
        value = str(filters.get(field_name) or '').strip()
        if value:
            clean_filters[field_name] = value[:max_length]

    person_role = str(filters.get('person_role') or 'any').strip()
    clean_filters['person_role'] = person_role if person_role in {'any', 'lead', 'contact'} else 'any'

    for field_name in ('start_year', 'effective_end_year'):
        year = _normalize_year(filters.get(field_name))
        if year:
            clean_filters[field_name] = year
    clean_filters['exclude_closed'] = filters.get('exclude_closed') is True

    clean_plan = {'action': raw_plan['action'], 'filters': clean_filters}
    if raw_plan['action'] == 'compare_project_counts':
        years = [_normalize_year(year) for year in (raw_plan.get('years') or [])]
        years = [year for year in years if year]
        if len(years) < 2:
            return None
        clean_plan['years'] = years[:2]
        clean_plan['filters'].pop('start_year', None)
        clean_plan['filters'].pop('effective_end_year', None)
    return clean_plan


def _ai_prompt(question: str, today: date) -> str:
    return f'''你是科研课题台账的查询意图解析器。当前日期是 {today.isoformat()}。
只把用户问题转换成 JSON 查询计划，不回答问题，不生成 SQL，也不要输出 Markdown。

允许的 action：
- list_projects：列出课题
- count_projects：统计课题数量
- compare_project_counts：按课题开始年份比较两个年份的课题数量

filters 只允许使用：
- person：人员姓名
- person_role：any、lead、contact
- ownership：西勘院、地下空间
- level：国家级、省部级、地市级、公司级
- project_type：应用研究、试验发展、全自筹课题
- role：牵头、参与
- status：未立项、在研、延期、结题、终止
- managing_unit：归口单位
- start_year：课题开始年份
- effective_end_year：计划结题年份，延期日期优先于原计划日期
- exclude_closed：用户问“要结题、待结题”时设为 true

比较问题必须输出 years，顺序保持用户的比较顺序，例如“26年比25年”输出 [2026, 2025]。
示例：{{"action":"list_projects","filters":{{"person":"张三","person_role":"any"}}}}
示例：{{"action":"list_projects","filters":{{"effective_end_year":{today.year},"exclude_closed":true}}}}
示例：{{"action":"compare_project_counts","filters":{{"level":"省部级"}},"years":[2026,2025]}}

用户问题：{question[:500]}
'''


def _request_ai_plan(question: str, today: date) -> tuple[dict[str, Any] | None, str]:
    prompt = _ai_prompt(question, today)
    configs = {
        config.service_name: config
        for config in APIConfig.objects.filter(is_active=True, test_success=True)
    }
    for service_name in ('deepseek', 'kimi'):
        config = configs.get(service_name)
        if not config:
            continue
        api_key = config.get_api_key()
        if not api_key:
            continue
        if service_name == 'deepseek':
            url = 'https://api.deepseek.com/v1/chat/completions'
            model = 'deepseek-v4-flash'
        else:
            url = 'https://api.moonshot.cn/v1/chat/completions'
            model = 'moonshot-v1-8k'
        try:
            response = requests.post(
                url,
                headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                json={
                    'model': model,
                    'messages': [{'role': 'user', 'content': prompt}],
                    'temperature': 0,
                    'max_tokens': 800,
                    **({'response_format': {'type': 'json_object'}} if service_name == 'deepseek' else {}),
                },
                timeout=30,
            )
            if response.status_code != 200:
                continue
            content = response.json()['choices'][0]['message']['content']
            plan = _validate_plan(_extract_json_object(content))
            if plan:
                return plan, config.get_service_name_display()
        except (KeyError, TypeError, ValueError, requests.RequestException):
            continue
    return None, ''


def parse_question(question: str, today: date | None = None, use_ai: bool = True) -> tuple[dict[str, Any] | None, str]:
    today = today or timezone.localdate()
    local_plan = _validate_plan(_local_plan(question, today))
    if local_plan:
        return local_plan, '本地语义解析'
    if use_ai:
        ai_plan, service_name = _request_ai_plan(question, today)
        if ai_plan:
            return ai_plan, f'{service_name} 意图解析'
    return None, ''


def _apply_filters(queryset: QuerySet[Project], filters: dict[str, Any]) -> QuerySet[Project]:
    for field_name in CHOICE_FILTERS:
        if filters.get(field_name):
            queryset = queryset.filter(**{field_name: filters[field_name]})
    if filters.get('managing_unit'):
        queryset = queryset.filter(managing_unit__icontains=filters['managing_unit'])
    if filters.get('start_year'):
        queryset = queryset.filter(start_year=filters['start_year'])
    if filters.get('effective_end_year'):
        year = filters['effective_end_year']
        queryset = queryset.filter(
            Q(extension_date__year=year)
            | Q(extension_date__isnull=True, planned_end_date__year=year)
        )
    if filters.get('person'):
        person = filters['person']
        person_role = filters.get('person_role', 'any')
        if person_role == 'lead':
            queryset = queryset.filter(project_lead__icontains=person)
        elif person_role == 'contact':
            queryset = queryset.filter(contact_person__icontains=person)
        else:
            queryset = queryset.filter(
                Q(project_lead__icontains=person) | Q(contact_person__icontains=person)
            )
    if filters.get('exclude_closed'):
        queryset = queryset.exclude(status__in={'结题', '终止'}).filter(actual_completion_date__isnull=True)
    return queryset


def _scope_text(filters: dict[str, Any], comparison: bool = False) -> str:
    parts = ['按课题开始年份统计'] if comparison else []
    for field_name, label in FILTER_LABELS.items():
        value = filters.get(field_name)
        if value:
            parts.append(f'{label}={value}')
    if filters.get('person'):
        role = filters.get('person_role', 'any')
        role_label = {'lead': '负责人', 'contact': '联系人', 'any': '负责人或联系人'}[role]
        parts[-1] = f'相关人员={filters["person"]}（{role_label}）'
    if filters.get('exclude_closed'):
        parts.append('排除已结题、已终止及已有实际结题日期的课题')
    return '；'.join(parts) if parts else '全部课题'


def _effective_end_date(project: Project):
    return project.extension_date or project.planned_end_date


def _person_roles(project: Project, person: str) -> str:
    roles = []
    if person and person in (project.project_lead or ''):
        roles.append('负责人')
    if person and person in (project.contact_person or ''):
        roles.append('联系人')
    return '、'.join(roles)


def execute_plan(plan: dict[str, Any], source: str = '') -> dict[str, Any]:
    filters = plan.get('filters') or {}
    action = plan['action']

    if action == 'compare_project_counts':
        target_year, base_year = plan['years'][:2]
        base_queryset = _apply_filters(Project.objects.all(), filters)
        target_count = base_queryset.filter(start_year=target_year).count()
        base_count = base_queryset.filter(start_year=base_year).count()
        difference = target_count - base_count
        growth_rate = (difference / base_count * 100) if base_count else None
        if difference > 0:
            change_text = f'增加 {difference} 项'
        elif difference < 0:
            change_text = f'减少 {abs(difference)} 项'
        else:
            change_text = '数量持平'
        rate_text = f'{growth_rate:.1f}%' if growth_rate is not None else '无法计算（基期为0）'
        return {
            'ok': True,
            'kind': 'comparison',
            'answer': (
                f'{target_year} 年符合条件的课题共 {target_count} 项，{base_year} 年共 {base_count} 项；'
                f'相比 {base_year} 年{change_text}，增长率为 {rate_text}。'
            ),
            'scope': _scope_text(filters, comparison=True),
            'source': source,
            'comparison': {
                'target_year': target_year,
                'target_count': target_count,
                'base_year': base_year,
                'base_count': base_count,
                'difference': difference,
                'growth_rate': growth_rate,
                'growth_rate_text': rate_text,
            },
        }

    queryset = _apply_filters(Project.objects.all(), filters)
    total = queryset.count()
    if action == 'count_projects':
        return {
            'ok': True,
            'kind': 'count',
            'answer': f'共查询到 {total} 项符合条件的课题。',
            'scope': _scope_text(filters),
            'source': source,
            'total': total,
        }

    projects = list(queryset.order_by('-start_year', 'project_id')[:MAX_RESULTS])
    if filters.get('effective_end_year'):
        projects.sort(key=lambda project: (_effective_end_date(project) or date.max, project.project_id))
    rows = [
        {
            'project': project,
            'effective_end_date': _effective_end_date(project),
            'person_roles': _person_roles(project, filters.get('person', '')),
        }
        for project in projects
    ]
    if filters.get('effective_end_year') and filters.get('exclude_closed'):
        answer = f'计划在 {filters["effective_end_year"]} 年结题且当前尚未结题或终止的课题共 {total} 项。'
    elif filters.get('person'):
        answer = f'查询到与“{filters["person"]}”相关的课题共 {total} 项。'
    else:
        answer = f'共查询到 {total} 项符合条件的课题。'
    if total > MAX_RESULTS:
        answer += f' 当前展示前 {MAX_RESULTS} 项。'
    return {
        'ok': True,
        'kind': 'project_list',
        'answer': answer,
        'scope': _scope_text(filters),
        'source': source,
        'total': total,
        'rows': rows,
    }


def answer_project_question(
    question: str,
    today: date | None = None,
    use_ai: bool = True,
    history: Any = None,
    service_name: str | None = None,
    funding_category: str | None = None,
) -> dict[str, Any]:
    question = (question or '').strip()
    if not question:
        return {'ok': False, 'error': '请输入一个课题查询问题。'}
    if len(question) > 500:
        return {'ok': False, 'error': '问题过长，请控制在500个字符以内。'}

    valid_categories = {key for key, _ in Project.FUNDING_CATEGORY_CHOICES}
    funding_category = funding_category if funding_category in valid_categories else None
    scoped_question = question
    if funding_category:
        scoped_question = (
            f'当前页面范围固定为“{dict(Project.FUNDING_CATEGORY_CHOICES)[funding_category]}”，'
            f'只查询该类别课题。用户问题：{question}'
        )

    if use_ai:
        from .ai_query_assistant import answer_with_ai

        ai_result = answer_with_ai(
            scoped_question,
            history=history,
            today=today,
            service_name=service_name,
            funding_category=funding_category,
        )
        if ai_result:
            ai_result['question'] = question
            return ai_result

    # 模型未配置或暂时不可用时，用本地白名单规则保证基础查询仍可工作。
    plan, source = parse_question(question, today=today, use_ai=False)
    if plan and funding_category:
        plan.setdefault('filters', {})['funding_category'] = funding_category
    if not plan:
        return {
            'ok': False,
            'error': '暂时没能理解这个问题。试用版支持按人员、年份、状态、级别查询，以及两个年份的数量和增长率比较。',
        }
    result = execute_plan(plan, source=source)
    result['question'] = question
    return result
