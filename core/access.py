import json
from pathlib import Path

from django.conf import settings


def is_system_admin(user):
    return bool(user and user.is_authenticated and (user.is_staff or user.is_superuser))


def readonly_download_enabled():
    config_file = Path(settings.BASE_DIR) / 'network_config.json'
    try:
        with config_file.open('r', encoding='utf-8') as file_obj:
            config = json.load(file_obj)
        return bool(config.get('readonly_can_download', False))
    except (OSError, ValueError, TypeError):
        return False


def can_download_files(user):
    if is_system_admin(user):
        return True
    return bool(user and user.is_authenticated and readonly_download_enabled())
