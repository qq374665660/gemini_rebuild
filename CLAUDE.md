# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Django-based research project management system (科研课题管理系统) designed to manage research projects throughout their lifecycle. The system combines structured database management with file system organization for comprehensive project documentation.

## Architecture

- **Backend**: Django 4.2.23 with Python
- **Database**: SQLite (db.sqlite3)
- **Frontend**: Django templates with HTML/CSS/JavaScript
- **Main App**: `core` - contains all project management functionality
- **Project Structure**: Single Django project with one main app

### Key Components

- **Models**: `core/models.py` - `Project` model with comprehensive research project fields
- **Views**: `core/views.py` - handles project CRUD, file management, Excel import, and statistics
- **Templates**: `core/templates/core/` - HTML templates for all views
- **File System Integration**: Automatic directory creation and management in `projects/` folder

## Development Commands

### Running the Application
```bash
python manage.py runserver
```

### Database Operations
```bash
# Apply migrations
python manage.py migrate

# Create migrations after model changes
python manage.py makemigrations

# Create superuser (for admin access)
python manage.py createsuperuser
```

### Development Setup
No requirements.txt found, but the project uses:
- Django 4.2.23
- openpyxl (for Excel import functionality)

## Core Business Logic

### Project Management
- Projects are identified by `project_id` (primary key)
- Each project has an associated directory structure in `projects/` folder
- Directory naming follows pattern: `{year}-{status}-{project_id}-{name}`
- When project status changes, the physical directory is automatically renamed

### File System Integration
- Automatic creation of standardized directory structure for each project:
  - `01_申报/`, `02_立项/`, `03_开题/`, `04_中期/`, `05_变更/`, `06_结题/`, `07_其它/`
- File upload/download capabilities through web interface
- Directory tree visualization in project detail view

### Data Import
- Excel import functionality from `科研课题管理总表.xlsx`
- Uses `update_or_create` pattern based on `project_id`
- Automatically creates directory structure for new projects

### Key Features
- Project listing with filtering by year and status
- Comprehensive project details with tabbed interface
- File management with tree view
- Statistics dashboard with charts
- Excel data import/export capabilities

## URL Structure

- `/` - Project list (homepage)
- `/project/create/` - Create new project
- `/project/{project_id}/` - Project detail view
- `/project/{project_id}/delete/` - Delete project
- `/project/{project_id}/file/{action}/` - File operations
- `/import/` - Excel import
- `/statistics/` - Statistics dashboard

## Important Notes

- System assumes no user authentication (open access)
- File operations are tightly integrated with database records
- Directory paths are automatically managed - users should not edit `directory_path` field directly
- Project deletion removes both database record and associated files
- Chinese language interface and field names throughout the system