# 科研课题管理系统 (Research Project Management System)

## 项目概述

这是一个基于Django的科研课题管理系统，用于管理科研项目的全生命周期。系统结合了结构化数据库管理和文件系统组织，为项目文档提供全面的管理功能。

## 技术架构

- **后端**: Django 4.2.23 with Python
- **数据库**: SQLite (db.sqlite3)
- **前端**: Django模板 + HTML/CSS/JavaScript
- **主要应用**: `core` - 包含所有项目管理功能

## 功能特性

### 核心功能
- 📊 **项目管理**: 创建、编辑、删除科研项目
- 📁 **文件管理**: 自动创建标准化项目目录结构
- 📈 **统计分析**: 项目统计和数据可视化
- 📤 **数据导入**: 支持Excel文件批量导入项目数据
- 🤖 **AI分析**: 集成AI服务进行项目内容分析

### 项目目录结构
系统自动为每个项目创建标准化目录结构：
```
项目文件夹/
├── 01_申报/
├── 02_立项/
├── 03_开题/
├── 04_中期/
├── 05_变更/
├── 06_结题/
└── 07_其它/
```

## 快速开始

### 环境要求
- Python 3.8+
- Django 4.2.23

### 安装步骤

1. **克隆项目**
   ```bash
   git clone <repository-url>
   cd gemini_rebuild
   ```

2. **安装依赖**
   ```bash
   pip install -r requirements.txt
   ```

3. **数据库迁移**
   ```bash
   python manage.py migrate
   ```

4. **启动开发服务器**
   ```bash
   python manage.py runserver
   ```

5. **访问系统**
   打开浏览器访问: http://127.0.0.1:10086/

## 数据模型

### Project (项目模型)
- **project_id**: 课题编号 (主键)
- **name**: 课题名称
- **ownership**: 课题归属 (西勘院/地下空间)
- **level**: 课题级别 (国家级/省部级/公司级)
- **project_type**: 课题类型 (应用研究/试验发展)
- **status**: 课题状态
- **start_year**: 开始年份
- **budget**: 预算信息
- **dates**: 各种时间节点

### ProjectAnalysis (项目分析模型)
- **project**: 关联项目
- **analysis_type**: 分析类型
- **analysis_result**: 分析结果
- **confidence_score**: 置信度分数

### APIConfig (API配置模型)
- **service_name**: AI服务名称
- **api_key**: API密钥
- **is_active**: 启用状态

## 主要页面

- **项目列表** (`/`): 显示所有项目的概览
- **项目详情** (`/project/<id>/`): 查看单个项目的详细信息
- **创建项目** (`/project/create/`): 创建新项目
- **统计分析** (`/statistics/`): 项目统计数据
- **数据导入** (`/import/`): Excel文件导入
- **API配置** (`/api-config/`): AI服务配置

## 开发指南

### 项目结构
```
gemini_rebuild/
├── core/                 # 主应用
│   ├── models.py        # 数据模型
│   ├── views.py         # 视图函数
│   ├── urls.py          # URL路由
│   ├── forms.py         # 表单定义
│   ├── templates/       # 模板文件
│   └── migrations/      # 数据库迁移
├── project_manager/     # Django项目配置
├── projects/            # 项目文件存储目录
├── db.sqlite3          # SQLite数据库
├── manage.py           # Django管理脚本
└── requirements.txt    # 依赖包列表
```

### 添加新功能
1. 在 `core/models.py` 中定义数据模型
2. 创建并运行数据库迁移
3. 在 `core/views.py` 中添加视图函数
4. 在 `core/urls.py` 中配置URL路由
5. 创建相应的模板文件

## 部署说明

### 生产环境配置
1. 设置 `DEBUG = False`
2. 配置 `ALLOWED_HOSTS`
3. 使用生产级数据库 (PostgreSQL/MySQL)
4. 配置静态文件服务
5. 设置安全密钥

## 许可证

本项目仅供内部使用。

## 联系方式

如有问题或建议，请联系开发团队。