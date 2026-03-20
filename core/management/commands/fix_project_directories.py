"""
Django管理命令：修复所有项目的目录结构
检查并修复所有项目的目录结构，确保符合标准规范
"""
from django.core.management.base import BaseCommand
from django.conf import settings
from core.models import Project
from core.views import create_project_directory_structure, _get_project_folder_name
import os
import shutil
import re


class Command(BaseCommand):
    help = '检查并修复所有项目的目录结构，确保符合标准规范'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='只检查不修复，显示需要修复的项目',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='强制重新创建所有目录结构（危险操作）',
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('开始检查项目目录结构...'))
        
        projects = Project.objects.all()
        self.stdout.write(f'总共找到 {projects.count()} 个项目')
        
        fixed_count = 0
        error_count = 0
        
        for project in projects:
            try:
                result = self.check_and_fix_project(project, options['dry_run'], options['force'])
                if result:
                    fixed_count += 1
            except Exception as e:
                error_count += 1
                self.stdout.write(
                    self.style.ERROR(f'处理项目 {project.project_id} 时出错: {e}')
                )
        
        if options['dry_run']:
            self.stdout.write(self.style.WARNING(f'检查完成：{fixed_count} 个项目需要修复'))
        else:
            self.stdout.write(self.style.SUCCESS(f'修复完成：{fixed_count} 个项目已修复，{error_count} 个出错'))

    def check_and_fix_project(self, project, dry_run=False, force=False):
        """检查并修复单个项目的目录结构"""
        project_id = project.project_id
        expected_folder_name = _get_project_folder_name(project)
        expected_path = os.path.join(settings.BASE_DIR, 'projects', expected_folder_name)
        
        needs_fix = False
        fix_actions = []
        
        # 检查1: directory_path字段是否正确
        if project.directory_path != expected_path:
            needs_fix = True
            fix_actions.append(f'更新directory_path: {project.directory_path} -> {expected_path}')
        
        # 检查2: 物理目录是否存在
        if not os.path.exists(expected_path):
            needs_fix = True
            fix_actions.append(f'创建项目目录: {expected_path}')
        
        # 检查3: 是否有旧的目录需要重命名
        projects_root = str(settings.PROJECTS_ROOT)
        if os.path.exists(projects_root):
            for item in os.listdir(projects_root):
                item_path = os.path.join(projects_root, item)
                if os.path.isdir(item_path) and item != expected_folder_name:
                    # 检查是否是这个项目的旧目录（包含项目ID）
                    if project_id in item:
                        needs_fix = True
                        fix_actions.append(f'重命名旧目录: {item} -> {expected_folder_name}')
                        break
        
        # 检查4: 标准子目录是否存在
        if os.path.exists(expected_path):
            required_dirs = [
                '01_申报', '02_立项', '03_开题及任务书',
                '04_中期', '05_变更', '06_结题', '07_其它',
            ]
            for dir_name in required_dirs:
                dir_path = os.path.join(expected_path, dir_name)
                if not os.path.exists(dir_path):
                    needs_fix = True
                    fix_actions.append(f'创建子目录: {dir_name}')
        
        if needs_fix:
            self.stdout.write(f'项目 {project_id} ({project.name}) 需要修复:')
            for action in fix_actions:
                self.stdout.write(f'  - {action}')
            
            if not dry_run:
                self.fix_project_directory(project, expected_path, force)
                self.stdout.write(self.style.SUCCESS(f'  ✅ 已修复'))
        
        return needs_fix

    def fix_project_directory(self, project, expected_path, force=False):
        """修复项目目录结构"""
        projects_root = os.path.join(settings.BASE_DIR, 'projects')
        
        # 查找并处理旧目录
        if os.path.exists(projects_root):
            for item in os.listdir(projects_root):
                item_path = os.path.join(projects_root, item)
                if (os.path.isdir(item_path) and 
                    item != os.path.basename(expected_path) and 
                    project.project_id in item):
                    
                    if os.path.exists(expected_path) and not force:
                        self.stdout.write(
                            self.style.WARNING(f'目标目录已存在，跳过重命名: {expected_path}')
                        )
                    else:
                        # 重命名旧目录
                        if os.path.exists(expected_path):
                            shutil.rmtree(expected_path)
                        os.rename(item_path, expected_path)
                        self.stdout.write(f'重命名目录: {item} -> {os.path.basename(expected_path)}')
                    break
        
        # 创建或更新目录结构
        create_project_directory_structure(project)
        
        # 更新数据库中的directory_path
        if project.directory_path != expected_path:
            project.directory_path = expected_path
            project.save(update_fields=['directory_path'])
