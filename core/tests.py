from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
import tempfile

import openpyxl
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import ProjectForm
from .models import Project
from .views import import_from_excel_view


class ProjectFilterTests(TestCase):
    def setUp(self):
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
