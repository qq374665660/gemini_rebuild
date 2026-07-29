from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
import tempfile
from unittest.mock import patch

import openpyxl
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import ProjectForm
from .models import OperationLog, Project
from .views import import_from_excel_view
from . import backup_schedule


class ProjectFilterTests(TestCase):
    def setUp(self):
        self.admin_user = get_user_model().objects.create_user('filter-admin', password='StrongPass!234', is_staff=True)
        self.client.force_login(self.admin_user)
        Project.objects.create(
            project_id='P1',
            name='Alpha Project',
            ownership='GroupA',
            managing_unit='UnitA',
            level='LevelA',
            project_type='TypeA',
            role='Lead',
            start_year=2023,
            status='In Progress',
            project_lead='LeaderA',
            start_date=date(2023, 1, 1),
            planned_end_date=date(2024, 12, 31),
            total_budget=Decimal('120.00'),
            directory_path='P1',
        )
        Project.objects.create(
            project_id='P2',
            name='Beta Project',
            ownership='GroupB',
            managing_unit='UnitB',
            level='LevelB',
            project_type='TypeB',
            role='Member',
            start_year=2024,
            status='Completed',
            project_lead='LeaderB',
            start_date=date(2024, 1, 15),
            planned_end_date=date(2025, 12, 31),
            total_budget=Decimal('15.00'),
            directory_path='P2',
        )
        Project.objects.create(
            project_id='P3',
            name='Gamma Project',
            ownership='GroupA',
            managing_unit='UnitC',
            level='LevelA',
            project_type='TypeA',
            role='Lead',
            start_year=2024,
            status='In Progress',
            project_lead='LeaderC',
            start_date=date(2024, 5, 1),
            planned_end_date=date(2026, 1, 1),
            total_budget=Decimal('80.00'),
            directory_path='P3',
        )
        Project.objects.create(
            project_id='P4',
            name='Delta Project',
            ownership='GroupA',
            managing_unit='',
            level='LevelA',
            project_type='TypeA',
            role='Lead',
            start_year=2024,
            status='In Progress',
            project_lead='LeaderD',
            start_date=date(2024, 2, 1),
            planned_end_date=date(2026, 6, 1),
            total_budget=Decimal('60.00'),
            directory_path='P4',
        )

    def test_query_terms_and_filter(self):
        response = self.client.get(reverse('project_list'), {'q': 'Alpha UnitA'})
        self.assertEqual(response.status_code, 200)
        projects = list(response.context['projects'])
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0].project_id, 'P1')

    def test_multi_select_and_budget_range(self):
        response = self.client.get(
            reverse('project_list'),
            {
                'year': ['2023', '2024'],
                'status': ['In Progress', 'Completed'],
                'min_budget': '50',
                'max_budget': '100',
            }
        )
        self.assertEqual(response.status_code, 200)
        projects = list(response.context['projects'])
        self.assertEqual([p.project_id for p in projects], ['P3', 'P4'])

    def test_blank_and_date_filters(self):
        response = self.client.get(
            reverse('project_list'),
            {
                'managing_unit': ['__blank__'],
                'start_date_from': '2024-01-01',
            }
        )
        self.assertEqual(response.status_code, 200)
        projects = list(response.context['projects'])
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0].project_id, 'P4')


class ProgressMonitorFilterTests(TestCase):
    def setUp(self):
        self.admin_user = get_user_model().objects.create_user('progress-admin', password='StrongPass!234', is_staff=True)
        self.client.force_login(self.admin_user)
        today = timezone.localdate()

        Project.objects.create(
            project_id='PM_OVERDUE',
            name='Overdue Project',
            ownership='GroupA',
            managing_unit='UnitA',
            level='LevelA',
            project_type='TypeA',
            role='Lead',
            start_year=today.year - 1,
            status='In Progress',
            start_date=today - timedelta(days=30),
            planned_end_date=today - timedelta(days=1),
            directory_path='PM_OVERDUE',
        )
        Project.objects.create(
            project_id='PM_MIDTERM',
            name='Midterm Project',
            ownership='GroupA',
            managing_unit='UnitA',
            level='LevelA',
            project_type='TypeA',
            role='Lead',
            start_year=today.year,
            status='In Progress',
            start_date=today - timedelta(days=20),
            planned_end_date=today + timedelta(days=20),
            directory_path='PM_MIDTERM',
        )
        Project.objects.create(
            project_id='PM_ONTRACK',
            name='OnTrack Project',
            ownership='GroupA',
            managing_unit='UnitA',
            level='LevelA',
            project_type='TypeA',
            role='Lead',
            start_year=today.year,
            status='In Progress',
            start_date=today - timedelta(days=10),
            planned_end_date=today + timedelta(days=40),
            directory_path='PM_ONTRACK',
        )

    def test_progress_status_filter_overdue(self):
        response = self.client.get(reverse('progress_monitor'), {'progress_status': 'overdue'})
        self.assertEqual(response.status_code, 200)

        rows = list(response.context['monitor_rows'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['project'].project_id, 'PM_OVERDUE')
        self.assertEqual(rows[0]['status_key'], 'overdue')
        self.assertEqual(response.context['selected_progress_status'], 'overdue')

    def test_progress_status_filter_midterm(self):
        response = self.client.get(reverse('progress_monitor'), {'progress_status': 'midterm'})
        self.assertEqual(response.status_code, 200)

        rows = list(response.context['monitor_rows'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['project'].project_id, 'PM_MIDTERM')
        self.assertEqual(rows[0]['status_key'], 'midterm')


class ProjectCreationTests(TestCase):
    def test_create_project_page_renders_required_role_field(self):
        html = render_to_string('core/create_project.html', {'form': ProjectForm()})

        self.assertIn('name="role"', html)

    def test_project_level_choices_include_city_level(self):
        form = ProjectForm()
        level_values = [value for value, _ in form.fields['level'].choices if value]

        self.assertEqual(level_values, ['国家级', '省部级', '地市级', '公司级'])

    def test_project_status_choices_are_limited_and_legacy_values_normalize(self):
        form = ProjectForm()
        status_values = [value for value, _ in form.fields['status'].choices if value]

        self.assertEqual(status_values, ['申报', '在研', '延期', '结题', '终止'])
        self.assertEqual(Project.normalize_status('未立项'), '申报')
        self.assertEqual(Project.normalize_status('立项'), '在研')
        self.assertEqual(Project.normalize_status('已立项'), '在研')
        self.assertEqual(Project.normalize_status('完成'), '结题')
        self.assertEqual(Project.normalize_status('未知状态'), '')

    def test_import_strips_text_fields_before_creating_directory(self):
        workbook = openpyxl.Workbook()
        worksheet = workbook.active
        worksheet.append([
            '课题编号', '课题名称', '课题归属', '归口单位', '课题级别',
            '课题类型', '参与角色', '开始年份', '课题状态',
        ])
        worksheet.append([
            'IMPORT-TRIM-1', '导入课题末尾空格 ', '西勘院', '测试单位', '公司级',
            '应用研究', '牵头', 2026, '在研',
        ])
        output = BytesIO()
        workbook.save(output)
        output.seek(0)
        upload = SimpleUploadedFile(
            'projects.xlsx',
            output.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )

        with tempfile.TemporaryDirectory() as projects_root:
            with override_settings(PROJECTS_ROOT=Path(projects_root)):
                request = RequestFactory().post(reverse('import_from_excel'), {'excel_file': upload})
                request.session = {}
                request._messages = FallbackStorage(request)
                response = import_from_excel_view(request)

                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.url, reverse('project_list'))
                project = Project.objects.get(project_id='IMPORT-TRIM-1')
                self.assertEqual(project.name, '导入课题末尾空格')
                self.assertEqual(Path(project.directory_path).name, '2026-在研-IMPORT-TRIM-1-导入课题末尾空格')
                self.assertTrue(Path(project.directory_path, '01_申报').is_dir())


class BackupScheduleTests(TestCase):
    weekly_task_payload = (
        '{"task_name":"KetiBackupWeekly","state":"Ready","enabled":true,'
        '"start_boundary":"2026-02-06T02:00:00","time":"02:00",'
        '"trigger_type":"Weekly","interval_days":7,'
        '"last_run_time":"2026-07-26 02:00:00",'
        '"next_run_time":"2026-08-02 02:00:00","last_result":0}'
    )
    daily_task_payload = (
        '{"task_name":"KetiBackupWeekly","state":"Ready","enabled":true,'
        '"start_boundary":"2026-07-28T03:30:00","time":"03:30",'
        '"trigger_type":"Daily","interval_days":3,'
        '"last_run_time":"2026-07-26 02:00:00",'
        '"next_run_time":"2026-07-31 03:30:00","last_result":0}'
    )

    def setUp(self):
        self.admin_user = get_user_model().objects.create_user('backup-admin', password='StrongPass!234', is_staff=True)
        self.client.force_login(self.admin_user)

    @override_settings(BACKUP_TASK_NAME=r'\KetiBackupWeekly', BACKUP_SCHEDULE_READ_ONLY=False)
    @patch('core.backup_schedule._run_powershell')
    def test_reads_weekly_backup_schedule(self, run_powershell):
        run_powershell.return_value = self.weekly_task_payload

        schedule = backup_schedule.get_backup_schedule()

        read_script = run_powershell.call_args.args[0]
        self.assertIn('MSFT_TaskDailyTrigger', read_script)
        self.assertIn('MSFT_TaskWeeklyTrigger', read_script)
        self.assertIn('WeeksInterval * 7', read_script)
        self.assertTrue(schedule['available'])
        self.assertEqual(schedule['trigger_type'], 'Weekly')
        self.assertEqual(schedule['interval_days'], 7)
        self.assertEqual(schedule['time'], '02:00')
        self.assertTrue(schedule['last_result_success'])

    @override_settings(BACKUP_TASK_NAME=r'\KetiBackupWeekly', BACKUP_SCHEDULE_READ_ONLY=False)
    @patch('core.backup_schedule._run_powershell')
    def test_updates_only_daily_interval_trigger(self, run_powershell):
        run_powershell.side_effect = ['', self.daily_task_payload]

        schedule = backup_schedule.update_backup_schedule('3', '03:30')

        update_script = run_powershell.call_args_list[0].args[0]
        self.assertIn('Set-ScheduledTask', update_script)
        self.assertIn('New-ScheduledTaskTrigger -Daily -DaysInterval 3', update_script)
        self.assertIn('AddHours(3).AddMinutes(30)', update_script)
        self.assertNotIn('DaysOfWeek', update_script)
        self.assertNotIn('backup_weekly.ps1', update_script)
        self.assertTrue(schedule['available'])
        self.assertEqual(schedule['interval_days'], 3)

    @override_settings(BACKUP_SCHEDULE_READ_ONLY=True)
    @patch('core.backup_schedule._run_powershell')
    def test_development_read_only_mode_blocks_updates(self, run_powershell):
        schedule = backup_schedule.get_backup_schedule()
        self.assertTrue(schedule['available'])
        self.assertTrue(schedule['read_only'])
        self.assertTrue(schedule['preview'])
        self.assertEqual(schedule['interval_days'], 7)
        with self.assertRaisesMessage(backup_schedule.BackupScheduleError, '开发环境为只读模式'):
            backup_schedule.update_backup_schedule('7', '02:00')
        run_powershell.assert_not_called()

    @override_settings(BACKUP_SCHEDULE_READ_ONLY=False)
    @patch('core.backup_schedule._run_powershell')
    def test_invalid_schedule_values_are_rejected_before_powershell(self, run_powershell):
        with self.assertRaisesMessage(backup_schedule.BackupScheduleError, '必须是整数天数'):
            backup_schedule.update_backup_schedule('three', '02:00')
        with self.assertRaisesMessage(backup_schedule.BackupScheduleError, '1 到 3650'):
            backup_schedule.update_backup_schedule('0', '02:00')
        with self.assertRaisesMessage(backup_schedule.BackupScheduleError, '格式为 HH:MM'):
            backup_schedule.update_backup_schedule('3', '25:99')
        run_powershell.assert_not_called()

    def test_settings_page_shows_adjust_backup_button(self):
        schedule = {
            'available': True,
            'read_only': False,
            'task_name': 'KetiBackupWeekly',
            'state': 'Ready',
            'enabled': True,
            'trigger_type': 'Weekly',
            'interval_days': 7,
            'time': '02:00',
            'last_run_time': '2026-07-26 02:00:00',
            'next_run_time': '2026-08-02 02:00:00',
            'last_result': 0,
            'last_result_success': True,
        }

        html = render_to_string(
            'core/settings.html',
            {
                'backup_schedule': schedule,
                'projects': [],
            },
        )

        self.assertIn('调整备份计划', html)
        self.assertIn('name="backup_interval_days"', html)
        self.assertIn('每隔 7 天，于 02:00 执行', html)

    @patch('core.views.backup_scheduler.update_backup_schedule')
    def test_settings_page_updates_backup_schedule(self, update_schedule):
        update_schedule.return_value = {'available': True}

        response = self.client.post(
            reverse('settings'),
            {
                'action': 'save_backup_schedule',
                'backup_interval_days': '3',
                'backup_time': '04:15',
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('settings'))
        update_schedule.assert_called_once_with('3', '04:15')


class AccessControlTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user('admin-user', password='StrongPass!234', is_staff=True)
        self.readonly = User.objects.create_user('readonly-user', password='StrongPass!234')
        self.project = Project.objects.create(
            project_id='ACCESS-1',
            name='权限测试课题',
            ownership='西勘院',
            managing_unit='测试单位',
            level='公司级',
            project_type='应用研究',
            role='牵头',
            start_year=2026,
            status='在研',
            directory_path='ACCESS-1',
        )

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse('project_list'))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse('login')))

    def test_readonly_user_can_view_content_but_not_admin_pages_or_writes(self):
        self.client.force_login(self.readonly)

        self.assertEqual(self.client.get(reverse('project_list')).status_code, 200)
        self.assertEqual(self.client.get(reverse('statistics')).status_code, 200)
        self.assertEqual(self.client.get(reverse('progress_monitor')).status_code, 200)
        self.assertEqual(self.client.get(reverse('settings')).status_code, 403)
        self.assertEqual(self.client.post(reverse('create_project'), {}).status_code, 403)

    def test_readonly_page_hides_admin_controls(self):
        self.client.force_login(self.readonly)

        response = self.client.get(reverse('project_list'))

        self.assertNotContains(response, '从Excel导入')
        self.assertNotContains(response, '新建课题')
        self.assertNotContains(response, 'API配置')
        self.assertContains(response, '只读')

    def test_readonly_preview_is_allowed_and_download_uses_global_switch(self):
        self.client.force_login(self.readonly)
        with tempfile.TemporaryDirectory() as config_root, tempfile.TemporaryDirectory() as projects_root:
            config_root_path = Path(config_root)
            project_root_path = Path(projects_root)
            folder = project_root_path / '2026-在研-ACCESS-1-权限测试课题'
            target_folder = folder / '01_申报'
            target_folder.mkdir(parents=True)
            (target_folder / '材料.txt').write_text('test', encoding='utf-8')
            self.project.directory_path = str(folder)
            self.project.save(update_fields=['directory_path'])
            config_file = config_root_path / 'network_config.json'
            config_file.write_text('{"readonly_can_download": false}', encoding='utf-8')

            with override_settings(BASE_DIR=config_root_path, PROJECTS_ROOT=project_root_path):
                detail_response = self.client.get(reverse('project_detail', args=[self.project.project_id]))
                self.assertEqual(detail_response.status_code, 200)
                self.assertNotContains(detail_response, '<h3>编辑信息</h3>', html=True)
                self.assertNotContains(detail_response, '上传到 <span id="upload-target-name">根目录</span>', html=True)
                self.assertFalse(detail_response.context['can_download_files'])
                preview_url = reverse('file_action', args=[self.project.project_id, 'preview']) + '?path=01_申报/材料.txt'
                download_url = reverse('file_action', args=[self.project.project_id, 'download']) + '?path=01_申报/材料.txt'
                preview_response = self.client.get(preview_url)
                self.assertEqual(preview_response.status_code, 200)
                preview_response.close()
                self.assertEqual(self.client.get(download_url).status_code, 403)

                config_file.write_text('{"readonly_can_download": true}', encoding='utf-8')
                download_response = self.client.get(download_url)
                self.assertEqual(download_response.status_code, 200)
                download_response.close()

    def test_admin_can_create_readonly_user_and_operation_is_logged(self):
        self.client.force_login(self.admin)

        response = self.client.post(reverse('user_management'), {
            'action': 'create',
            'username': 'new-reader',
            'display_name': '只读测试用户',
            'role': 'readonly',
            'password1': 'AnotherPass!234',
            'password2': 'AnotherPass!234',
        })

        self.assertEqual(response.status_code, 302)
        created_user = get_user_model().objects.get(username='new-reader')
        self.assertFalse(created_user.is_staff)
        self.assertTrue(created_user.is_active)
        self.assertTrue(OperationLog.objects.filter(user=self.admin, action_name='user_management').exists())
