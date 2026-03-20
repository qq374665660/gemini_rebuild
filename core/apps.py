from django.apps import AppConfig
import os


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'
    
    def ready(self):
        """Django应用启动时自动执行的方法"""
        # 避免在迁移和其他管理命令时执行
        import sys
        if 'runserver' in sys.argv or 'gunicorn' in sys.argv[0] if sys.argv else False:
            self.check_and_create_project_directories()
    
    def check_and_create_project_directories(self):
        """检查并创建所有缺失的项目目录"""
        try:
            from django.conf import settings
            from .models import Project
            from .views import create_project_directory_structure
            
            print("[系统启动] 开始检查项目目录结构...")
            
            # 确保projects根目录存在
            projects_root = str(settings.PROJECTS_ROOT)
            os.makedirs(projects_root, exist_ok=True)
            
            # 获取所有项目
            projects = Project.objects.all()
            created_count = 0
            
            for project in projects:
                try:
                    # 检查项目目录是否存在
                    if not project.directory_path or not os.path.exists(project.directory_path):
                        print(f"[系统启动] 为项目 '{project.name}' 创建目录结构...")
                        create_project_directory_structure(project)
                        created_count += 1
                        print(f"[系统启动] ✅ 已创建: {project.directory_path}")
                except Exception as e:
                    print(f"[系统启动] ❌ 创建项目 '{project.name}' 目录时出错: {e}")
            
            if created_count > 0:
                print(f"[系统启动] 目录检查完成，共为 {created_count} 个项目创建了目录结构")
            else:
                print(f"[系统启动] 目录检查完成，所有项目目录都已存在")
                
        except Exception as e:
            print(f"[系统启动] 项目目录检查过程中出错: {e}")
