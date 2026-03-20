from django.urls import path
from . import views

urlpatterns = [
    path('', views.project_list_view, name='project_list'),
    path('init/', views.init_system_view, name='init_system'),
    path('import/', views.import_from_excel_view, name='import_from_excel'),
    path('export/', views.export_project_list_view, name='export_project_list'),
    path('statistics/', views.statistics_view, name='statistics'),
    path('progress/', views.progress_monitor_view, name='progress_monitor'),
    path('expense/', views.expense_monitor_view, name='expense_monitor'),
    path('expense/import/', views.expense_import_view, name='expense_import'),
    path('expense/mapping/', views.expense_mapping_view, name='expense_mapping'),
    path('project/create/', views.create_project_view, name='create_project'),
    path('project/extract-task-docx/', views.extract_task_docx_view, name='extract_task_docx'),
    path('project/<str:project_id>/', views.project_detail_view, name='project_detail'),
    path('project/<str:project_id>/file-manager-test/', views.file_manager_trial_view, name='file_manager_trial'),
    path('project/<str:project_id>/delete/', views.delete_project_view, name='delete_project'),
    path('project/<str:project_id>/file/<str:action>/', views.file_action_view, name='file_action'),
    path('project/<str:project_id>/analyze/<str:analysis_type>/', views.analyze_content_view, name='analyze_content'),
    path('project/<str:project_id>/edit-analysis/<str:analysis_type>/', views.edit_analysis_view, name='edit_analysis'),
    path('project/<str:project_id>/metrics-item/<int:item_id>/update/', views.update_metrics_item_view, name='update_metrics_item'),
    path('project/<str:project_id>/file-tree/', views.get_file_tree_view, name='get_file_tree'),
    path('api-config/', views.api_config_view, name='api_config'),
    path('settings/', views.settings_view, name='settings'),
    path('test_upload/', views.test_upload_view, name='test_upload'),
    path('api/network-config/', views.get_network_config_api, name='get_network_config'),
]
