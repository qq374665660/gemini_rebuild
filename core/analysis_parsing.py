"""任务书分析结果的结构化解析与展示文本生成。"""

from __future__ import annotations

import json
import re
from typing import Any


METRIC_CATEGORY_ALIASES = {
    'deliverable': 'deliverable',
    'task': 'deliverable',
    '考核指标': 'deliverable',
    '任务书考核指标': 'deliverable',
    'benefit': 'benefit',
    '经济社会指标': 'benefit',
    '经济与社会效益': 'benefit',
    '主要经济、社会指标': 'benefit',
    'milestone': 'milestone',
    '阶段目标': 'milestone',
    '阶段进度目标': 'milestone',
    '进度计划': 'milestone',
    'technical': 'technical',
    '技术成果': 'technical',
    'academic': 'academic',
    '学术成果': 'academic',
    'standard': 'standard',
    '标准制定': 'standard',
    'talent': 'talent',
    '人才培养': 'talent',
    'economic': 'economic',
    '经济效益': 'economic',
    'other': 'other',
    '其他': 'other',
}


def _as_text(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def extract_json_payload(raw_text: str) -> dict:
    """从模型响应中提取首个完整 JSON 对象。"""
    if not raw_text:
        return {}
    text = raw_text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*```$', '', text)
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        pass

    start = text.find('{')
    if start < 0:
        return {}
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                try:
                    payload = json.loads(text[start:index + 1])
                    return payload if isinstance(payload, dict) else {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    return {}
    return {}


def normalize_research_payload(payload: dict) -> dict:
    """把不同模型的研究内容 JSON 统一为页面所需结构。"""
    if not isinstance(payload, dict):
        return {}
    sections = []
    for order, section in enumerate(
        _as_list(payload.get('research_sections') or payload.get('sections') or payload.get('research_content')),
        start=1,
    ):
        if isinstance(section, str):
            sections.append({'title': f'研究内容 {order}', 'description': section, 'subitems': []})
            continue
        if not isinstance(section, dict):
            continue
        subitems = []
        for subitem in _as_list(section.get('subitems') or section.get('items')):
            if isinstance(subitem, str):
                subitems.append({'title': '', 'description': subitem})
            elif isinstance(subitem, dict):
                subitems.append({
                    'title': _as_text(subitem.get('title') or subitem.get('name')),
                    'description': _as_text(subitem.get('description') or subitem.get('content')),
                })
        sections.append({
            'title': _as_text(section.get('title') or section.get('name') or f'研究内容 {order}'),
            'description': _as_text(section.get('description') or section.get('content')),
            'subitems': subitems,
        })

    milestones = []
    for item in _as_list(payload.get('milestones') or payload.get('progress_plan')):
        if isinstance(item, str):
            milestones.append({'time_range': '', 'stage_goal': item})
        elif isinstance(item, dict):
            milestones.append({
                'time_range': _as_text(item.get('time_range') or item.get('planned_period') or item.get('time')),
                'stage_goal': _as_text(item.get('stage_goal') or item.get('goal') or item.get('description')),
            })

    return {
        'research_goal': _as_text(payload.get('research_goal') or payload.get('goal')),
        'summary': _as_text(payload.get('summary')),
        'research_sections': sections,
        'methods': [_as_text(item) for item in _as_list(payload.get('methods')) if _as_text(item)],
        'technical_route': [_as_text(item) for item in _as_list(payload.get('technical_route')) if _as_text(item)],
        'innovations': [_as_text(item) for item in _as_list(payload.get('innovations')) if _as_text(item)],
        'technical_difficulties': [
            _as_text(item) for item in _as_list(payload.get('technical_difficulties')) if _as_text(item)
        ],
        'milestones': milestones,
    }


def normalize_metric_payload(payload: dict) -> dict:
    """保留任务书指标原文，并统一目标、考核方式和计划字段。"""
    if not isinstance(payload, dict):
        return {'summary': '', 'metrics': []}
    raw_items = payload.get('metrics') or payload.get('items') or payload.get('output_metrics') or []
    metrics = []
    for order, item in enumerate(_as_list(raw_items), start=1):
        if isinstance(item, str):
            item = {'indicator_description': item}
        if not isinstance(item, dict):
            continue
        category_raw = _as_text(item.get('category') or item.get('metric_type') or 'other')
        category = METRIC_CATEGORY_ALIASES.get(category_raw, METRIC_CATEGORY_ALIASES.get(
            category_raw.replace('指标', ''), 'other'
        ))
        description = _as_text(
            item.get('indicator_description') or item.get('description') or item.get('item_name') or item.get('name')
        )
        if not description:
            continue
        metrics.append({
            'category': category,
            'item_name': description,
            'target_value': _as_text(item.get('target_value') or item.get('quantity') or item.get('target')),
            'assessment_method': _as_text(item.get('assessment_method') or item.get('verification_method')),
            'planned_period': _as_text(item.get('planned_period') or item.get('time_range') or item.get('promotion_time')),
            'deadline': _as_text(item.get('deadline')),
            'source_section': _as_text(item.get('source_section') or item.get('section')),
            'source_page': _as_text(item.get('source_page') or item.get('page')),
            'notes': _as_text(item.get('notes') or item.get('expected_benefit')),
            'sort_order': order,
        })
    return {'summary': _as_text(payload.get('summary')), 'metrics': metrics}


def parse_metrics_text(raw_text: str) -> list[dict]:
    """解析结构化 JSON；兼容历史 Markdown 列表。"""
    payload = extract_json_payload(raw_text)
    normalized = normalize_metric_payload(payload)
    if normalized['metrics']:
        return normalized['metrics']

    category = 'other'
    items = []
    heading_aliases = sorted(METRIC_CATEGORY_ALIASES.items(), key=lambda pair: len(pair[0]), reverse=True)
    for line in (raw_text or '').splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for label, code in heading_aliases:
            if label and label in stripped and not stripped.startswith(('-', '*', '•')):
                category = code
                break
        if not stripped.startswith(('-', '*', '•')):
            continue
        item_text = stripped.lstrip('-*• ').strip()
        parts = re.split(r'[：:]', item_text, maxsplit=1)
        description = parts[0].strip()
        target = parts[1].strip() if len(parts) > 1 else ''
        if description:
            items.append({
                'category': category,
                'item_name': description,
                'target_value': target,
                'assessment_method': '',
                'planned_period': '',
                'deadline': '',
                'source_section': '',
                'source_page': '',
                'notes': '',
                'sort_order': len(items) + 1,
            })
    return items


def format_research_markdown(data: dict) -> str:
    sections = []
    if data.get('research_goal'):
        sections.extend(['一、课题研究目标', data['research_goal']])
    if data.get('research_sections'):
        sections.append('二、课题研究内容')
        for index, section in enumerate(data['research_sections'], start=1):
            sections.append(f"{index}. {section.get('title') or '研究内容'}")
            if section.get('description'):
                sections.append(section['description'])
            for subitem in section.get('subitems') or []:
                label = f"- {subitem.get('title')}：" if subitem.get('title') else '- '
                sections.append(f"{label}{subitem.get('description', '')}")
    for title, key in (
        ('三、研究方法', 'methods'),
        ('四、技术路线', 'technical_route'),
        ('五、技术难点', 'technical_difficulties'),
        ('六、创新与突破', 'innovations'),
    ):
        values = data.get(key) or []
        if values:
            sections.append(title)
            sections.extend(f'- {value}' for value in values)
    if data.get('milestones'):
        sections.append('七、进度计划')
        for item in data['milestones']:
            sections.append(f"- {item.get('time_range') or '时间待定'}：{item.get('stage_goal', '')}")
    return '\n\n'.join(part for part in sections if part)


def format_metrics_markdown(data: dict) -> str:
    category_labels = {
        'deliverable': '任务书考核指标',
        'benefit': '主要经济、社会指标',
        'milestone': '课题进度计划',
        'technical': '技术成果指标',
        'academic': '学术成果指标',
        'standard': '标准制定指标',
        'talent': '人才培养指标',
        'economic': '经济效益指标',
        'other': '其他指标',
    }
    grouped = {}
    for item in data.get('metrics') or []:
        grouped.setdefault(item.get('category', 'other'), []).append(item)
    lines = []
    if data.get('summary'):
        lines.extend(['产出指标概述', data['summary']])
    for category, items in grouped.items():
        lines.append(category_labels.get(category, '其他指标'))
        for item in items:
            details = []
            if item.get('target_value'):
                details.append(f"目标：{item['target_value']}")
            if item.get('assessment_method'):
                details.append(f"考核方式：{item['assessment_method']}")
            if item.get('planned_period'):
                details.append(f"计划时间：{item['planned_period']}")
            suffix = f"（{'；'.join(details)}）" if details else ''
            lines.append(f"- {item.get('item_name', '')}{suffix}")
    return '\n\n'.join(lines)


def parse_ai_analysis(raw_text: str, analysis_type: str) -> tuple[str, dict, list[dict]]:
    """返回展示文本、结构化数据和指标明细。"""
    payload = extract_json_payload(raw_text)
    if analysis_type == 'research_content':
        structured = normalize_research_payload(payload)
        display = format_research_markdown(structured) if any(structured.values()) else raw_text
        return display, structured, []
    structured = normalize_metric_payload(payload)
    metrics = structured.get('metrics') or parse_metrics_text(raw_text)
    if metrics and not structured.get('metrics'):
        structured['metrics'] = metrics
    display = format_metrics_markdown(structured) if metrics else raw_text
    return display, structured, metrics
