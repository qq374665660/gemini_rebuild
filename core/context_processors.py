from .access import can_download_files, is_system_admin


def access_control(request):
    try:
        from .ai_providers import get_model_name, ready_configs

        ai_model_options = [
            {
                'service_name': config.service_name,
                'service_label': config.get_service_name_display(),
                'model_name': get_model_name(config),
            }
            for config in ready_configs()
        ]
    except Exception:
        # 迁移执行期间或数据库暂不可用时，不影响登录页和其他页面渲染。
        ai_model_options = []
    return {
        'is_system_admin': is_system_admin(request.user),
        'can_download_files': can_download_files(request.user),
        'ai_model_options': ai_model_options,
    }
