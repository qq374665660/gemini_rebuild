# MySQL 数据库部署指南

本指南将帮助您将项目从 SQLite 迁移到 MySQL 数据库。

## 1. MySQL 安装与配置

### 1.1 下载和安装 MySQL

1. 访问 [MySQL 官网](https://dev.mysql.com/downloads/mysql/)
2. 下载 MySQL Community Server（推荐版本：8.0 或 5.7）
3. 运行安装程序，选择 "Developer Default" 安装类型
4. 设置 root 用户密码（请记住此密码）
5. 配置 MySQL 服务为开机自启动

### 1.2 验证安装

```bash
# 检查 MySQL 服务状态
net start mysql

# 登录 MySQL
mysql -u root -p
```

### 1.3 创建项目数据库和用户

```sql
-- 创建数据库
CREATE DATABASE research_project_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- 创建专用用户
CREATE USER 'research_user'@'localhost' IDENTIFIED BY 'your_secure_password';

-- 授权
GRANT ALL PRIVILEGES ON research_project_db.* TO 'research_user'@'localhost';
FLUSH PRIVILEGES;

-- 退出
EXIT;
```

## 2. Python 环境配置

### 2.1 安装 MySQL 客户端库

```bash
# 使用 uv 安装 mysqlclient
uv add mysqlclient

# 或者使用 pip
pip install mysqlclient
```

**注意：** 在 Windows 上，如果安装 mysqlclient 遇到问题，可以尝试：

```bash
# 安装预编译的轮子
pip install mysqlclient --only-binary=all

# 或者使用 PyMySQL 作为替代
uv add PyMySQL
```

### 2.2 更新 Django 设置

修改 `project_manager/settings.py`：

```python
# 原来的 SQLite 配置（注释掉）
# DATABASES = {
#     'default': {
#         'ENGINE': 'django.db.backends.sqlite3',
#         'NAME': BASE_DIR / 'db.sqlite3',
#     }
# }

# 新的 MySQL 配置
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': 'research_project_db',
        'USER': 'research_user',
        'PASSWORD': 'your_secure_password',
        'HOST': 'localhost',
        'PORT': '3306',
        'OPTIONS': {
            'charset': 'utf8mb4',
            'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
        },
    }
}
```

如果使用 PyMySQL，还需要在 `project_manager/__init__.py` 中添加：

```python
import pymysql
pymysql.install_as_MySQLdb()
```

## 3. 数据迁移

### 3.1 备份现有数据

```bash
# 导出现有 SQLite 数据
python manage.py dumpdata --natural-foreign --natural-primary -e contenttypes -e auth.Permission > data_backup.json
```

### 3.2 重新创建迁移文件

```bash
# 删除现有迁移文件（保留 __init__.py）
rm core/migrations/0*.py

# 创建新的初始迁移
python manage.py makemigrations core

# 应用迁移
python manage.py migrate
```

### 3.3 导入数据

```bash
# 导入备份的数据
python manage.py loaddata data_backup.json
```

### 3.4 创建超级用户（如果需要）

```bash
python manage.py createsuperuser
```

## 4. 性能优化配置

### 4.1 MySQL 配置优化

编辑 MySQL 配置文件（通常在 `C:\ProgramData\MySQL\MySQL Server 8.0\my.ini`）：

```ini
[mysqld]
# 基本设置
port = 3306
basedir = "C:/Program Files/MySQL/MySQL Server 8.0/"
datadir = "C:/ProgramData/MySQL/MySQL Server 8.0/Data/"

# 字符集设置
character-set-server = utf8mb4
collation-server = utf8mb4_unicode_ci

# 性能优化
innodb_buffer_pool_size = 256M
innodb_log_file_size = 64M
max_connections = 200
query_cache_size = 32M
query_cache_type = 1

# 日志设置
log-error = "C:/ProgramData/MySQL/MySQL Server 8.0/Data/mysql_error.log"
general-log = 0
slow-query-log = 1
slow-query-log-file = "C:/ProgramData/MySQL/MySQL Server 8.0/Data/mysql_slow.log"
long_query_time = 2
```

### 4.2 Django 数据库优化

在 `settings.py` 中添加：

```python
# 数据库连接池配置
DATABASES['default']['CONN_MAX_AGE'] = 60

# 查询优化
DATABASES['default']['OPTIONS'].update({
    'autocommit': True,
    'use_unicode': True,
})
```

## 5. 监控和维护

### 5.1 数据库备份脚本

创建 `backup_mysql.bat`：

```batch
@echo off
set BACKUP_DIR=C:\backups\mysql
set DATE=%date:~0,4%%date:~5,2%%date:~8,2%
set TIME=%time:~0,2%%time:~3,2%%time:~6,2%
set BACKUP_FILE=%BACKUP_DIR%\research_project_db_%DATE%_%TIME%.sql

if not exist %BACKUP_DIR% mkdir %BACKUP_DIR%

mysqldump -u research_user -p research_project_db > %BACKUP_FILE%

echo Backup completed: %BACKUP_FILE%
pause
```

### 5.2 定期维护任务

```sql
-- 优化表
OPTIMIZE TABLE core_project, core_projectanalysis, core_apiconfig, core_metricsitem;

-- 检查表
CHECK TABLE core_project, core_projectanalysis, core_apiconfig, core_metricsitem;

-- 分析表
ANALYZE TABLE core_project, core_projectanalysis, core_apiconfig, core_metricsitem;
```

### 5.3 监控查询

```sql
-- 查看慢查询
SELECT * FROM mysql.slow_log ORDER BY start_time DESC LIMIT 10;

-- 查看数据库大小
SELECT 
    table_schema AS 'Database',
    ROUND(SUM(data_length + index_length) / 1024 / 1024, 2) AS 'Size (MB)'
FROM information_schema.tables 
WHERE table_schema = 'research_project_db'
GROUP BY table_schema;

-- 查看表大小
SELECT 
    table_name AS 'Table',
    ROUND(((data_length + index_length) / 1024 / 1024), 2) AS 'Size (MB)'
FROM information_schema.TABLES 
WHERE table_schema = 'research_project_db'
ORDER BY (data_length + index_length) DESC;
```

## 6. 故障排除

### 6.1 常见问题

**问题 1：连接被拒绝**
```bash
# 检查 MySQL 服务状态
net start mysql

# 检查端口是否开放
netstat -an | findstr 3306
```

**问题 2：字符编码问题**
```sql
-- 检查字符集设置
SHOW VARIABLES LIKE 'character_set%';
SHOW VARIABLES LIKE 'collation%';
```

**问题 3：权限问题**
```sql
-- 检查用户权限
SHOW GRANTS FOR 'research_user'@'localhost';
```

### 6.2 回滚到 SQLite

如果需要回滚到 SQLite：

1. 备份 MySQL 数据：
```bash
python manage.py dumpdata --natural-foreign --natural-primary -e contenttypes -e auth.Permission > mysql_backup.json
```

2. 恢复 SQLite 配置：
```python
# 在 settings.py 中恢复 SQLite 配置
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}
```

3. 重新迁移和导入数据：
```bash
python manage.py migrate
python manage.py loaddata mysql_backup.json
```

## 7. 安全建议

1. **密码安全**：使用强密码，定期更换
2. **网络安全**：限制 MySQL 只监听本地连接
3. **用户权限**：遵循最小权限原则
4. **定期备份**：设置自动备份任务
5. **更新维护**：定期更新 MySQL 版本

## 8. 性能对比

| 特性 | SQLite | MySQL |
|------|--------|-------|
| 并发读取 | 良好 | 优秀 |
| 并发写入 | 有限 | 优秀 |
| 数据大小限制 | 281TB | 无限制 |
| 内存使用 | 低 | 中等 |
| 配置复杂度 | 简单 | 中等 |
| 备份恢复 | 简单 | 专业 |
| 网络访问 | 不支持 | 支持 |

---

**注意事项：**
- 迁移前请务必备份所有数据
- 在测试环境中先验证迁移过程
- 迁移过程中可能需要停机维护
- 建议在业务低峰期进行迁移