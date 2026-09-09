"""支出监控的月度明细解析与课题归并逻辑。"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
import hashlib
from pathlib import Path
import re
import unicodedata


EXPENSE_FORMAT_VERSION = '6606-cumulative-debit-v1'
EXPENSE_ACCOUNT_CODE = Decimal('6606')
EXPENSE_UNIT_DIVISOR = Decimal('10000')
EXPENSE_UNIT_LABEL = '万元'
EXPENSE_REQUIRED_COLUMNS = (
    '公司名称',
    '总账科目',
    '利润中心名称',
    '科研课题文本描述',
    '本年累计借方金额',
)
FULL_COMPANY_NAME = '中国建筑西南勘察设计研究院有限公司'
SHORT_COMPANY_NAME = '中建西勘院'
TARGET_COMPANIES = (
    FULL_COMPANY_NAME,
    '中建地下空间有限公司',
)
PROFIT_CENTER_LAST = f'{SHORT_COMPANY_NAME}-本部'


class ExpenseWorkbookError(ValueError):
    """上传工作簿不符合支出监控格式。"""


def abbreviate_company_name(value):
    if value is None:
        return ''
    return str(value).replace(FULL_COMPANY_NAME, SHORT_COMPANY_NAME)


def clean_match_text(value):
    if value is None:
        return ''
    text_value = unicodedata.normalize('NFKC', str(value)).strip().lower()
    if not text_value:
        return ''
    text_value = re.sub(r'\s+', '', text_value)
    return re.sub(r'[^0-9a-zA-Z\u4e00-\u9fff]+', '', text_value)


def canonicalize_expense_description(value):
    """移除课题名末尾的经费来源标记，保留技术内容中的正常括号。"""
    if value is None:
        return ''
    text_value = unicodedata.normalize('NFKC', str(value)).strip()
    if not text_value:
        return ''

    funding_parenthetical = re.compile(
        r'\s*\([^()]*?(?:自筹|专项经费|专项资金|公司专项|院专项|单位专项|'
        r'配套经费|配套资金|经费来源|资金来源)[^()]*?\)\s*$'
    )
    separated_funding_suffix = re.compile(
        r'\s*[-—–_/、，,;；:：]\s*'
        r'(?:(?:[\u4e00-\u9fffA-Za-z0-9]+)?自筹|'
        r'(?:院|公司|单位|部门)?专项(?:经费|资金)?|配套(?:经费|资金))\s*$'
    )
    project_suffix = re.compile(r'\s*[-—–_/、，,;；:：]?\s*课题项目\s*$')

    while text_value:
        original = text_value
        text_value = project_suffix.sub('', text_value).strip()
        text_value = funding_parenthetical.sub('', text_value).strip()
        text_value = separated_funding_suffix.sub('', text_value).strip()
        text_value = text_value.rstrip(' -—–_/、，,;；:：')
        if text_value == original:
            break
    return text_value


def normalized_expense_description(value):
    canonical = canonicalize_expense_description(value)
    return clean_match_text(canonical or value)


def similarity_score(left, right):
    if not left or not right:
        return 0.0
    if left in right or right in left:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def file_sha256(file_path):
    digest = hashlib.sha256()
    with Path(file_path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _cell_text(value, pandas_module=None):
    if value is None:
        return ''
    if pandas_module is not None:
        try:
            if pandas_module.isna(value):
                return ''
        except (TypeError, ValueError):
            pass
    text_value = unicodedata.normalize('NFKC', str(value)).strip()
    if text_value.lower() in {'nan', 'none', 'nat'}:
        return ''
    return text_value


def _is_6606_account(value, pandas_module=None):
    account = _cell_text(value, pandas_module=pandas_module)
    if not account:
        return False
    account = re.sub(r'[\s,，]', '', account)
    try:
        return Decimal(account) == EXPENSE_ACCOUNT_CODE
    except InvalidOperation:
        return False


def _decimal_amount(value, pandas_module=None):
    if value is None:
        return Decimal('0'), False
    if pandas_module is not None:
        try:
            if pandas_module.isna(value):
                return Decimal('0'), False
        except (TypeError, ValueError):
            pass
    if isinstance(value, Decimal):
        return value, False
    text_value = unicodedata.normalize('NFKC', str(value)).strip()
    if not text_value:
        return Decimal('0'), False
    negative = text_value.startswith('(') and text_value.endswith(')')
    if negative:
        text_value = text_value[1:-1]
    text_value = text_value.replace(',', '').replace('，', '').replace('¥', '').replace('￥', '')
    try:
        amount = Decimal(text_value)
    except InvalidOperation:
        return Decimal('0'), True
    return (-amount if negative else amount), False


def inspect_expense_workbook(file_path):
    """返回第一个符合新月度明细格式的工作表名。"""
    try:
        import pandas as pd
    except Exception as exc:  # pragma: no cover - 仅服务部署缺依赖时触发
        raise ExpenseWorkbookError('服务端缺少 pandas，无法读取 Excel。') from exc

    missing_by_sheet = []
    try:
        with pd.ExcelFile(file_path) as workbook:
            for sheet_name in workbook.sheet_names:
                try:
                    header = pd.read_excel(workbook, sheet_name=sheet_name, nrows=0)
                except Exception as exc:
                    missing_by_sheet.append(f'{sheet_name}（读取失败：{exc}）')
                    continue
                columns = {str(column).strip() for column in header.columns}
                missing = [column for column in EXPENSE_REQUIRED_COLUMNS if column not in columns]
                if not missing:
                    return sheet_name
                missing_by_sheet.append(f'{sheet_name}（缺少：{"、".join(missing)}）')
    except ExpenseWorkbookError:
        raise
    except Exception as exc:
        raise ExpenseWorkbookError(f'无法打开 Excel 文件：{exc}') from exc

    detail = '；'.join(missing_by_sheet) if missing_by_sheet else '工作簿中没有工作表'
    raise ExpenseWorkbookError(
        '未找到可用的月度支出明细表。必须包含：'
        f'{"、".join(EXPENSE_REQUIRED_COLUMNS)}。{detail}'
    )


def _project_completion_date(project):
    return (
        getattr(project, 'actual_completion_date', None)
        or getattr(project, 'extension_date', None)
        or getattr(project, 'planned_end_date', None)
    )


def _company_breakdown(company_totals):
    company_order = {name: index for index, name in enumerate(TARGET_COMPANIES)}
    return [
        {
            'raw_name': name,
            'name': abbreviate_company_name(name) or '未填写公司',
            'total': total,
        }
        for name, total in sorted(
            company_totals.items(),
            key=lambda item: (company_order.get(item[0], len(company_order)), item[0]),
        )
        if total != 0
    ]


def _sorted_profit_centers(profit_centers):
    return sorted(
        {abbreviate_company_name(name).strip() for name in profit_centers if str(name).strip()},
        key=lambda name: (name == PROFIT_CENTER_LAST, name),
    )


def analyze_expense_workbook(file_path, projects, mappings=(), threshold=0.85):
    """按6606、本年累计借方金额和标准化课题名分析一个月度工作簿。"""
    try:
        import pandas as pd
    except Exception as exc:  # pragma: no cover - 仅服务部署缺依赖时触发
        raise ExpenseWorkbookError('服务端缺少 pandas，无法读取 Excel。') from exc

    sheet_name = inspect_expense_workbook(file_path)
    try:
        dataframe = pd.read_excel(file_path, sheet_name=sheet_name)
    except Exception as exc:
        raise ExpenseWorkbookError(f'读取工作表“{sheet_name}”失败：{exc}') from exc
    dataframe.columns = [str(column).strip() for column in dataframe.columns]

    candidates = []
    project_lookup = {}
    for project in projects:
        normalized_name = normalized_expense_description(getattr(project, 'name', ''))
        if not normalized_name:
            continue
        project_id = getattr(project, 'project_id')
        info = {
            'project_id': project_id,
            'project_name': getattr(project, 'name', ''),
            'normalized_name': normalized_name,
            'budget_value': getattr(project, 'total_budget', None),
            'completion_date': _project_completion_date(project),
            'funding_category': getattr(project, 'funding_category', 'special'),
            'funding_category_label': project.get_funding_category_display() if hasattr(project, 'get_funding_category_display') else '',
        }
        candidates.append(info)
        project_lookup[project_id] = info

    mapping_map = {}
    for mapping in mappings:
        normalized = getattr(mapping, 'normalized_text', '')
        mapped_project = getattr(mapping, 'project', None)
        if normalized and mapped_project is not None:
            mapping_map[normalized] = getattr(mapped_project, 'project_id')

    description_groups = {}
    company_totals = defaultdict(lambda: Decimal('0'))
    company_row_counts = defaultdict(int)
    negative_rows = []
    filtered_rows = 0
    invalid_amount_total = 0

    for row in dataframe.to_dict('records'):
        if not _is_6606_account(row.get('总账科目'), pandas_module=pd):
            continue
        filtered_rows += 1
        company = _cell_text(row.get('公司名称'), pandas_module=pd) or '未填写公司'
        profit_center = abbreviate_company_name(
            _cell_text(row.get('利润中心名称'), pandas_module=pd)
        )
        description = _cell_text(row.get('科研课题文本描述'), pandas_module=pd)
        amount_yuan, invalid_amount = _decimal_amount(row.get('本年累计借方金额'), pandas_module=pd)
        if invalid_amount:
            invalid_amount_total += 1
        amount = amount_yuan / EXPENSE_UNIT_DIVISOR
        company_totals[company] += amount
        company_row_counts[company] += 1

        if amount < 0:
            negative_rows.append({
                'company': abbreviate_company_name(company),
                'profit_center': profit_center or '-',
                'description': abbreviate_company_name(description) or '-',
                'amount': amount,
            })

        normalized = normalized_expense_description(description)
        group_key = normalized or '__blank_description__'
        group = description_groups.setdefault(group_key, {
            'normalized': normalized,
            'canonical_description': abbreviate_company_name(
                canonicalize_expense_description(description)
            ),
            'variants': [],
            'variant_normalized': set(),
            'profit_centers': set(),
            'total': Decimal('0'),
            'row_count': 0,
            'company_totals': defaultdict(lambda: Decimal('0')),
        })
        raw_normalized = clean_match_text(description)
        display_description = abbreviate_company_name(description)
        if display_description and display_description not in group['variants']:
            group['variants'].append(display_description)
        if raw_normalized:
            group['variant_normalized'].add(raw_normalized[:512])
        if profit_center:
            group['profit_centers'].add(profit_center)
        group['total'] += amount
        group['row_count'] += 1
        group['company_totals'][company] += amount

    project_entries = {}
    unmatched_rows = []
    unmatched_company_totals = defaultdict(lambda: Decimal('0'))

    for group in description_groups.values():
        normalized = group['normalized']
        best_score = 0.0
        best_project_id = None
        match_method = 'auto'

        manual_project_id = mapping_map.get(normalized)
        if manual_project_id is None:
            for variant_normalized in group['variant_normalized']:
                manual_project_id = mapping_map.get(variant_normalized)
                if manual_project_id is not None:
                    break
        if manual_project_id in project_lookup:
            best_project_id = manual_project_id
            best_score = 1.0
            match_method = 'manual'
        elif normalized:
            for candidate in candidates:
                score = similarity_score(normalized, candidate['normalized_name'])
                if score > best_score:
                    best_score = score
                    best_project_id = candidate['project_id']
                    if best_score == 1.0:
                        break

        sample_description = '；'.join(group['variants'][:3]) or '-'
        if best_project_id is not None and best_score >= threshold:
            project_info = project_lookup[best_project_id]
            entry = project_entries.setdefault(best_project_id, {
                **project_info,
                'total': Decimal('0'),
                'max_score': best_score,
                'sample_desc': sample_description,
                'row_count': 0,
                'variant_count': 0,
                'profit_centers': set(),
                'company_totals': defaultdict(lambda: Decimal('0')),
                'match_method': match_method,
            })
            entry['total'] += group['total']
            entry['row_count'] += group['row_count']
            entry['variant_count'] += max(len(group['variants']), 1)
            entry['profit_centers'].update(group['profit_centers'])
            for company, total in group['company_totals'].items():
                entry['company_totals'][company] += total
            if best_score >= entry['max_score']:
                entry['max_score'] = best_score
                entry['sample_desc'] = sample_description
            if match_method == 'manual':
                entry['match_method'] = 'manual'
            continue

        for company, total in group['company_totals'].items():
            unmatched_company_totals[company] += total
        company_breakdown = _company_breakdown(group['company_totals'])
        unmatched_rows.append({
            'company': '；'.join(item['name'] for item in company_breakdown) or '-',
            'company_breakdown': company_breakdown,
            'profit_centers': _sorted_profit_centers(group['profit_centers']),
            'description': sample_description,
            'canonical_description': group['canonical_description'] or '-',
            'amount': group['total'],
            'best_name': project_lookup[best_project_id]['project_name'] if best_project_id else '-',
            'best_score': best_score,
            'row_count': group['row_count'],
            'variant_count': max(len(group['variants']), 1),
        })

    project_rows = []
    over_budget_alerts = []
    missing_budget_rows = []
    matched_company_totals = defaultdict(lambda: Decimal('0'))
    for entry in project_entries.values():
        entry['profit_centers'] = _sorted_profit_centers(entry['profit_centers'])
        entry['profit_center_names'] = '；'.join(entry['profit_centers']) or '-'
        entry['company_breakdown'] = _company_breakdown(entry.pop('company_totals'))
        entry['company_names'] = '；'.join(item['name'] for item in entry['company_breakdown']) or '-'
        for item in entry['company_breakdown']:
            matched_company_totals[item['raw_name']] += item['total']
        budget_value = entry['budget_value']
        entry['over_budget'] = budget_value is not None and entry['total'] > budget_value
        entry['over_amount'] = entry['total'] - budget_value if entry['over_budget'] else Decimal('0')
        if entry['over_budget']:
            over_budget_alerts.append(entry)
        if budget_value is None:
            missing_budget_rows.append(entry)
        project_rows.append(entry)

    project_rows.sort(key=lambda item: (item['total'], item['project_name']), reverse=True)
    over_budget_alerts.sort(key=lambda item: (item['over_amount'], item['total']), reverse=True)
    missing_budget_rows.sort(key=lambda item: item['total'], reverse=True)
    unmatched_rows.sort(key=lambda item: item['amount'], reverse=True)
    negative_rows.sort(key=lambda item: item['amount'])

    company_summaries = []
    for company in TARGET_COMPANIES:
        total = company_totals[company]
        matched_total = matched_company_totals[company]
        company_summaries.append({
            'company': abbreviate_company_name(company),
            'company_raw': company,
            'total': total,
            'matched_total': matched_total,
            'unmatched_total': total - matched_total,
            'row_count': company_row_counts[company],
        })

    other_company_totals = defaultdict(lambda: Decimal('0'))
    for company, total in company_totals.items():
        if company not in TARGET_COMPANIES and total != 0:
            other_company_totals[abbreviate_company_name(company)] += total
    other_company_totals = dict(other_company_totals)
    total_expense_sum = sum(company_totals.values(), Decimal('0'))

    return {
        'sheet_name': sheet_name,
        'source_row_count': len(dataframe.index),
        'filtered_rows': filtered_rows,
        'ignored_account_rows': len(dataframe.index) - filtered_rows,
        'description_group_total': len(description_groups),
        'project_rows': project_rows,
        'matched_project_total': len(project_rows),
        'unmatched_rows': unmatched_rows,
        'negative_rows': negative_rows,
        'over_budget_alerts': over_budget_alerts,
        'missing_budget_rows': missing_budget_rows,
        'company_summaries': company_summaries,
        'other_company_totals': other_company_totals,
        'other_company_total': sum(other_company_totals.values(), Decimal('0')),
        'total_expense_sum': total_expense_sum,
        'invalid_amount_total': invalid_amount_total,
    }
