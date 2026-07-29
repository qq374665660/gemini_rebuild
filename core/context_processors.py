from .access import can_download_files, is_system_admin


def access_control(request):
    return {
        'is_system_admin': is_system_admin(request.user),
        'can_download_files': can_download_files(request.user),
    }
