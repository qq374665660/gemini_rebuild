"""统一管理 OpenAI 兼容的大模型配置和请求。"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

from .models import APIConfig


PROVIDER_DEFAULTS = {
    'deepseek': {
        'label': 'DeepSeek',
        'base_url': 'https://api.deepseek.com',
        'model': 'deepseek-v4-flash',
        'requires_key': True,
    },
    'kimi': {
        'label': 'Kimi (Moonshot)',
        'base_url': 'https://api.moonshot.cn',
        'model': 'moonshot-v1-8k',
        'requires_key': True,
    },
    'local': {
        'label': '本地模型',
        'base_url': 'http://192.168.0.182:8000',
        'model': 'XKY-AI',
        'requires_key': False,
    },
}

PROVIDER_ORDER = ('deepseek', 'kimi', 'local')


def provider_defaults(service_name: str) -> dict[str, Any]:
    return PROVIDER_DEFAULTS.get(service_name, {})


def get_base_url(config: APIConfig) -> str:
    return (config.base_url or provider_defaults(config.service_name).get('base_url') or '').strip().rstrip('/')


def get_model_name(config: APIConfig) -> str:
    return (config.model_name or provider_defaults(config.service_name).get('model') or '').strip()


def chat_completions_url(config: APIConfig) -> str:
    """接受服务根地址、/v1 地址或完整 chat/completions 地址。"""
    base_url = get_base_url(config)
    if not base_url:
        return ''
    parsed = urlsplit(base_url)
    path = parsed.path.rstrip('/')
    if path.endswith('/chat/completions'):
        final_path = path
    elif path.endswith('/v1'):
        final_path = f'{path}/chat/completions'
    else:
        final_path = f'{path}/v1/chat/completions'
    return urlunsplit((parsed.scheme, parsed.netloc, final_path, '', ''))


def models_url(config: APIConfig) -> str:
    base_url = get_base_url(config)
    if not base_url:
        return ''
    parsed = urlsplit(base_url)
    path = parsed.path.rstrip('/')
    if path.endswith('/chat/completions'):
        path = path[:-len('/chat/completions')]
    if not path.endswith('/v1'):
        path = f'{path}/v1'
    return urlunsplit((parsed.scheme, parsed.netloc, f'{path}/models', '', ''))


def request_headers(config: APIConfig) -> dict[str, str]:
    headers = {'Content-Type': 'application/json'}
    api_key = config.get_api_key().strip()
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    return headers


def config_is_usable(config: APIConfig) -> bool:
    defaults = provider_defaults(config.service_name)
    if not config.is_active or not get_base_url(config) or not get_model_name(config):
        return False
    if defaults.get('requires_key') and not config.get_api_key().strip():
        return False
    return True


def ready_configs(service_name: str | None = None) -> list[APIConfig]:
    queryset = APIConfig.objects.filter(is_active=True, test_success=True)
    if service_name:
        queryset = queryset.filter(service_name=service_name)
    configs = {config.service_name: config for config in queryset}
    return [configs[name] for name in PROVIDER_ORDER if name in configs and config_is_usable(configs[name])]


def post_chat_completion(config: APIConfig, payload: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    response = requests.post(
        chat_completions_url(config),
        headers=request_headers(config),
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def provider_payload(config: APIConfig, **values: Any) -> dict[str, Any]:
    payload = {'model': get_model_name(config), **values}
    # DeepSeek 的 thinking 扩展字段不发送给其他 OpenAI 兼容服务。
    if config.service_name == 'deepseek':
        payload.setdefault('thinking', {'type': 'disabled'})
    return payload

