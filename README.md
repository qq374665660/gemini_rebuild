# 科研课题管理系统

一个基于 Django 的科研课题管理系统，用于管理课题台账、项目目录、进度统计、经费导入与 AI 辅助分析。

当前仓库已清理本地运行数据，适合作为公开代码仓库使用；数据库、日志、业务文件目录和本机网络配置均不会被提交。

## 功能概览

- 课题列表、详情、创建、删除
- 自动生成标准化项目目录结构
- 课题统计与进度监控
- 经费数据导入、映射与快照管理
- DOCX 任务书内容提取
- AI 分析配置与内容分析
- 本地目录与网络共享路径切换

## 技术栈

- Python 3.13+
- Django 4.2.23
- SQLite
- openpyxl / pandas
- requests / cryptography
- waitress / whitenoise

## 主要页面

- `/`：课题列表
- `/project/create/`：创建课题
- `/project/<id>/`：课题详情
- `/statistics/`：统计分析
- `/progress/`：进度监控
- `/expense/`：经费监控
- `/api-config/`：AI 配置
- `/settings/`：系统设置

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/qq374665660/gemini_rebuild.git
cd gemini_rebuild
```

### 2. 创建虚拟环境并安装依赖

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
```

### 3. 配置环境变量

推荐复制 `.env.example`，或手动设置以下变量：

```bash
set DJANGO_SECRET_KEY=replace-with-a-real-secret
set DEEPSEEK_API_KEY=
set KIMI_API_KEY=
```

说明：

- `DJANGO_SECRET_KEY`：必填，公开部署时必须改成你自己的值
- `DEEPSEEK_API_KEY`：可选，用于 DeepSeek 分析能力
- `KIMI_API_KEY`：可选，用于 Kimi 分析能力

### 4. 准备本地配置

如果你需要网络共享目录功能，请复制示例文件：

```bash
copy network_config.example.json network_config.json
```

然后按实际环境修改 `network_config.json`。

### 5. 初始化数据库

```bash
python manage.py migrate
python manage.py createsuperuser
```

### 6. 启动开发服务器

项目自定义了 `runserver` 默认端口，直接执行：

```bash
python manage.py runserver
```

默认地址为：

```text
http://127.0.0.1:10086/
```

## 生产运行说明

仓库中提供了 Windows 服务和运维脚本：

- `django_service.py`：Windows 服务入口
- `scripts/restart_research_service.ps1`：服务重启脚本
- `scripts/backup_weekly.ps1`：项目目录与 SQLite 备份脚本

服务模式下默认通过 `waitress` 监听：

```text
0.0.0.0:1027
```

## 目录说明

```text
gemini_rebuild/
├── core/                    # 主应用：模型、视图、模板、静态资源
├── project_manager/         # Django 配置
├── scripts/                 # 运维与辅助脚本
├── manage.py                # Django 管理入口
├── requirements.txt         # 依赖列表
├── network_config.example.json
└── .env.example
```

## 未提交到仓库的本地数据

以下内容默认已加入 `.gitignore`：

- `db.sqlite3`
- `projects/`
- `staticfiles/`
- `django_service.log`
- `network_config.json`
- `_vendor/`

这样可以避免把真实业务数据、日志和机器配置提交到公开仓库。

## 当前状态

这是一个仍在持续整理中的个人自用项目，当前仓库更偏向“可运行源码 + 运维脚本 + 使用说明”的公开版本。

如果你想继续完善公开展示，下一步通常会做这几件事：

- 补截图或演示 GIF
- 增加 License
- 增加 `.env.example` 之外的部署模板
- 补自动化测试与 CI
