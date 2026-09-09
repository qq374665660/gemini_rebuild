from django.conf import settings
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect
from django.urls import Resolver404, resolve
from django.utils.http import urlencode

from .access import can_download_files, is_system_admin


class AccessControlMiddleware:
    """强制登录，并将非管理员限制为只读访问。"""

    public_prefixes = ('/login/', '/static/', '/favicon.ico')
    readonly_get_views = {
        'project_list',
        'project_detail',
        'statistics',
        'query_assistant',
        'progress_monitor',
        'expense_monitor',
        'get_file_tree',
    }
    account_views = {'logout', 'password_change', 'password_change_done'}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated and not request.path.startswith(self.public_prefixes):
            query = urlencode({'next': request.get_full_path()})
            return redirect(f'{settings.LOGIN_URL}?{query}')

        match = self._resolve(request)
        denied = self._readonly_denied(request, match)
        if denied:
            return denied

        response = self.get_response(request)
        self._write_audit_log(request, response, match)
        return response

    @staticmethod
    def _resolve(request):
        try:
            return resolve(request.path_info)
        except Resolver404:
            return None

    def _readonly_denied(self, request, match):
        if not request.user.is_authenticated or is_system_admin(request.user):
            return None

        view_name = match.url_name if match else ''
        if view_name in self.account_views:
            return None
        if view_name == 'query_assistant' and request.method == 'POST':
            # 助手 POST 仅执行白名单只读工具；使用 POST 避免问题和对话历史进入 URL 日志。
            return None
        if view_name in self.readonly_get_views and request.method in {'GET', 'HEAD', 'OPTIONS'}:
            return None
        if view_name == 'file_action' and request.method in {'GET', 'HEAD'}:
            action = (match.kwargs or {}).get('action')
            if action == 'preview':
                return None
            if action == 'download' and can_download_files(request.user):
                return None
        return self._forbidden_response(request)

    @staticmethod
    def _forbidden_response(request):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.path.endswith('/file-tree/'):
            return JsonResponse({'success': False, 'message': '当前账号为只读用户，无权执行此操作。'}, status=403)
        return HttpResponseForbidden('当前账号为只读用户，无权执行此操作。')

    @staticmethod
    def _write_audit_log(request, response, match):
        if not request.user.is_authenticated:
            return
        view_name = match.url_name if match else ''
        sensitive_get = view_name in {'export_project_list'} or (
            view_name == 'file_action' and (match.kwargs or {}).get('action') == 'download'
        )
        if request.method in {'GET', 'HEAD', 'OPTIONS'} and not sensitive_get:
            return
        try:
            from .models import OperationLog

            forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR', '')
            ip_address = (forwarded_for.split(',')[0].strip() if forwarded_for else request.META.get('REMOTE_ADDR')) or None
            OperationLog.objects.create(
                user=request.user,
                username=request.user.get_username(),
                method=request.method,
                path=request.get_full_path()[:500],
                action_name=view_name[:100],
                status_code=response.status_code,
                ip_address=ip_address,
            )
        except Exception:
            # 日志失败不能影响正常业务请求。
            pass
