# Windows Server 2016 部署指南

本指南将帮助您在 Windows Server 2016 上部署科研课题管理系统，并配置为内网可访问的服务。

## 1. 环境准备

### 1.1 安装 Python
1. 下载 Python 3.8+ 版本：https://www.python.org/downloads/windows/
2. 安装时勾选 "Add Python to PATH"
3. 验证安装：
   ```cmd
   python --version
   pip --version
   ```

### 1.2 安装 Git（可选）
1. 下载 Git for Windows：https://git-scm.com/download/win
2. 安装并配置

## 2. 项目部署

### 2.1 复制项目文件
1. 将整个项目文件夹复制到服务器，建议路径：`C:\inetpub\research_management`
2. 或使用 Git 克隆（如果有代码仓库）

### 2.2 安装依赖
```cmd
cd C:\inetpub\research_management
pip install -r requirements.txt
```

### 2.3 配置数据库
```cmd
python manage.py migrate
```

### 2.4 创建超级用户（可选）
```cmd
python manage.py createsuperuser
```

### 2.5 收集静态文件
```cmd
python manage.py collectstatic --noinput
```

## 3. 网络配置

### 3.1 修改 Django 设置
编辑 `project_manager/settings.py`：

```python
# 允许所有主机访问
ALLOWED_HOSTS = ['*']

# 或者指定具体的IP地址
# ALLOWED_HOSTS = ['192.168.0.50', 'localhost', '127.0.0.1']

# 生产环境设置
DEBUG = False

# 静态文件设置
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
```

### 3.2 配置服务器IP地址
1. 打开 "网络和共享中心"
2. 点击 "更改适配器设置"
3. 右键网络连接 → "属性"
4. 选择 "Internet 协议版本 4 (TCP/IPv4)" → "属性"
5. 设置静态IP地址，例如：
   - IP地址：192.168.0.50
   - 子网掩码：255.255.255.0
   - 默认网关：192.168.0.1
   - DNS服务器：192.168.0.1

### 3.3 配置防火墙
1. 打开 "Windows Defender 防火墙"
2. 点击 "高级设置"
3. 创建入站规则：
   - 规则类型：端口
   - 协议：TCP
   - 端口：1027
   - 操作：允许连接
   - 配置文件：全部勾选
   - 名称：Django Research Management

## 4. 后台运行配置

### 方法一：使用 Windows 服务（推荐）

#### 4.1 安装 pywin32
```cmd
pip install pywin32
```

#### 4.2 创建服务脚本
创建文件 `C:\inetpub\research_management\django_service.py`：

```python
import win32serviceutil
import win32service
import win32event
import socket
import subprocess
import os
import sys

class DjangoService(win32serviceutil.ServiceFramework):
    _svc_name_ = "ResearchManagementService"
    _svc_display_name_ = "Research Management Django Service"
    _svc_description_ = "科研课题管理系统服务"
    
    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        socket.setdefaulttimeout(60)
        self.process = None
    
    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.hWaitStop)
        if self.process:
            self.process.terminate()
    
    def SvcDoRun(self):
        import servicemanager
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, '')
        )
        
        # 设置工作目录
        os.chdir(r'C:\inetpub\research_management')
        
        # 启动Django服务器
        self.process = subprocess.Popen([
            sys.executable, 'manage.py', 'runserver', '0.0.0.0:1027'
        ])
        
        # 等待停止信号
        win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE)

if __name__ == '__main__':
    win32serviceutil.HandleCommandLine(DjangoService)
```

#### 4.3 安装和启动服务
```cmd
# 以管理员身份运行命令提示符
cd C:\inetpub\research_management
python django_service.py install
python django_service.py start
```

#### 4.4 服务管理命令
```cmd
# 停止服务
python django_service.py stop

# 重启服务
python django_service.py restart

# 卸载服务
python django_service.py remove
```

### 方法二：使用任务计划程序

#### 4.1 创建启动脚本
创建文件 `C:\inetpub\research_management\start_server.bat`：

```batch
@echo off
cd /d C:\inetpub\research_management
python manage.py runserver 0.0.0.0:1027
```

#### 4.2 配置任务计划
1. 打开 "任务计划程序"
2. 创建基本任务：
   - 名称：Research Management Server
   - 触发器：当计算机启动时
   - 操作：启动程序
   - 程序：`C:\inetpub\research_management\start_server.bat`
3. 在 "条件" 选项卡中取消勾选 "只有在计算机使用交流电源时才启动此任务"
4. 在 "设置" 选项卡中勾选 "如果请求后任务还在运行，强行将其停止"

### 方法三：使用 NSSM（Non-Sucking Service Manager）

#### 4.1 下载 NSSM
1. 下载：https://nssm.cc/download
2. 解压到 `C:\nssm`
3. 将 `C:\nssm\win64` 添加到系统 PATH

#### 4.2 创建服务
```cmd
# 以管理员身份运行
nssm install ResearchManagement
```

在弹出的窗口中配置：
- Path: `C:\Python\python.exe`（Python安装路径）
- Startup directory: `C:\inetpub\research_management`
- Arguments: `manage.py runserver 0.0.0.0:1027`

#### 4.3 启动服务
```cmd
nssm start ResearchManagement
```

## 5. 生产环境优化（可选）

### 5.1 使用 Gunicorn + Nginx

#### 安装 Gunicorn
```cmd
pip install gunicorn
```

#### 创建 Gunicorn 配置
创建文件 `gunicorn_config.py`：

```python
bind = "0.0.0.0:1027"
workers = 4
worker_class = "sync"
worker_connections = 1000
timeout = 30
keepalive = 2
max_requests = 1000
max_requests_jitter = 100
preload_app = True
```

#### 使用 Gunicorn 启动
```cmd
gunicorn --config gunicorn_config.py project_manager.wsgi:application
```

### 5.2 配置 Nginx（如果需要）
1. 下载 Nginx for Windows
2. 配置反向代理到 Django 应用

## 6. 访问测试

### 6.1 本地测试
在服务器上打开浏览器，访问：
- http://localhost:1027
- http://127.0.0.1:1027

### 6.2 内网测试
在内网其他计算机上访问：
- http://192.168.0.50:1027

## 7. 故障排除

### 7.1 常见问题

**问题1：无法访问**
- 检查防火墙设置
- 确认IP地址配置正确
- 检查 ALLOWED_HOSTS 设置

**问题2：静态文件无法加载**
- 运行 `python manage.py collectstatic`
- 检查 STATIC_ROOT 和 STATIC_URL 设置

**问题3：数据库错误**
- 确认数据库文件权限
- 重新运行 `python manage.py migrate`

### 7.2 日志查看

**Windows 服务日志：**
- 事件查看器 → Windows 日志 → 应用程序

**Django 日志：**
在 settings.py 中配置日志记录：

```python
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'file': {
            'level': 'INFO',
            'class': 'logging.FileHandler',
            'filename': 'C:\\inetpub\\research_management\\django.log',
        },
    },
    'loggers': {
        'django': {
            'handlers': ['file'],
            'level': 'INFO',
            'propagate': True,
        },
    },
}
```

## 8. 维护和更新

### 8.1 定期备份
- 备份数据库文件：`db.sqlite3`
- 备份项目文件夹
- 备份上传的文件
- 备份配置文件：`settings.py`

### 8.2 程序更新维护指南

当程序有修改时，需要按照以下步骤进行维护更新：

#### 8.2.1 准备工作

**1. 创建维护窗口**
- 通知用户系统将进行维护
- 选择业务影响最小的时间段
- 预估维护时间

**2. 完整备份**
```cmd
# 创建备份目录（以日期命名）
mkdir C:\backup\research_management_%date:~0,4%%date:~5,2%%date:~8,2%

# 备份整个项目目录
xcopy C:\inetpub\research_management C:\backup\research_management_%date:~0,4%%date:~5,2%%date:~8,2%\ /E /I

# 特别备份数据库
copy C:\inetpub\research_management\db.sqlite3 C:\backup\db_backup_%date:~0,4%%date:~5,2%%date:~8,2%.sqlite3
```

#### 8.2.2 更新流程

**步骤1：停止服务**
```cmd
# 如果使用Windows服务
python django_service.py stop

# 如果使用NSSM
nssm stop ResearchManagement

# 如果使用任务计划程序，需要手动停止任务
```

**步骤2：更新代码**
```cmd
cd C:\inetpub\research_management

# 方法1：如果使用Git
git stash  # 暂存本地修改
git pull origin main  # 拉取最新代码
git stash pop  # 恢复本地修改（如有冲突需手动解决）

# 方法2：如果是手动复制文件
# 将新的代码文件覆盖到项目目录，注意保留配置文件
```

**步骤3：检查和更新依赖**
```cmd
# 检查requirements.txt是否有变化
fc requirements.txt C:\backup\research_management_%date:~0,4%%date:~5,2%%date:~8,2%\requirements.txt

# 如果有新依赖，安装更新
pip install -r requirements.txt --upgrade

# 检查是否有不兼容的依赖
pip check
```

**步骤4：数据库迁移**
```cmd
# 检查是否有新的迁移文件
python manage.py showmigrations

# 执行数据库迁移
python manage.py migrate

# 如果有数据迁移问题，可以使用以下命令检查
python manage.py migrate --plan
```

**步骤5：更新静态文件**
```cmd
# 清理旧的静态文件
rmdir /s /q staticfiles

# 重新收集静态文件
python manage.py collectstatic --noinput
```

**步骤6：配置文件检查**
```cmd
# 检查Django配置
python manage.py check

# 检查部署配置
python manage.py check --deploy
```

**步骤7：测试启动**
```cmd
# 先进行测试启动
python manage.py runserver 127.0.0.1:8001

# 在浏览器中访问 http://127.0.0.1:8001 测试功能
# 确认无误后按 Ctrl+C 停止测试服务器
```

**步骤8：重启生产服务**
```cmd
# 启动正式服务
python django_service.py start

# 或者使用NSSM
nssm start ResearchManagement
```

#### 8.2.3 更新后验证

**1. 功能测试**
- 访问主页：http://服务器IP:1027
- 测试登录功能
- 测试项目创建和查看
- 测试文件上传下载
- 测试"打开项目文件夹"功能

**2. 性能检查**
```cmd
# 检查服务状态
sc query ResearchManagementService

# 检查端口监听
netstat -an | findstr :1027

# 检查进程
tasklist | findstr python
```

**3. 日志检查**
```cmd
# 查看Django日志
type C:\inetpub\research_management\django.log

# 查看Windows事件日志
eventvwr.msc
```

#### 8.2.4 回滚计划

如果更新后出现问题，可以快速回滚：

```cmd
# 停止当前服务
python django_service.py stop

# 删除当前版本
rmdir /s /q C:\inetpub\research_management

# 恢复备份版本
xcopy C:\backup\research_management_%date:~0,4%%date:~5,2%%date:~8,2%\ C:\inetpub\research_management\ /E /I

# 重启服务
python django_service.py start
```

#### 8.2.5 常见更新场景

**场景1：仅修改Python代码**
- 只需执行步骤1、2、6、7、8
- 无需数据库迁移和依赖更新

**场景2：修改数据库模型**
- 必须执行完整流程
- 特别注意步骤4的数据库迁移
- 建议先在测试环境验证迁移脚本

**场景3：添加新功能模块**
- 执行完整流程
- 可能需要新的依赖包
- 可能需要新的静态文件

**场景4：修改前端样式**
- 重点关注步骤5的静态文件收集
- 可能需要清理浏览器缓存

#### 8.2.6 维护最佳实践

1. **版本控制**
   - 使用Git管理代码版本
   - 为每次发布打标签
   - 记录详细的变更日志

2. **测试环境**
   - 在测试环境先验证更新
   - 使用相同的数据进行测试
   - 确保所有功能正常后再更新生产环境

3. **监控告警**
   - 设置服务监控
   - 配置异常告警
   - 定期检查系统资源使用情况

4. **文档维护**
   - 记录每次更新的详细步骤
   - 更新用户手册
   - 维护故障处理文档

### 8.3 定期维护任务

**每周任务：**
- 检查服务运行状态
- 查看错误日志
- 检查磁盘空间

**每月任务：**
- 完整数据备份
- 检查系统更新
- 性能监控报告

**每季度任务：**
- 依赖包安全更新
- 系统安全检查
- 备份策略评估

## 9. 安全建议

1. **定期更新系统和软件**
2. **使用强密码**
3. **限制网络访问**（如果不需要外网访问）
4. **定期备份数据**
5. **监控系统资源使用情况**
6. **配置 HTTPS**（生产环境推荐）

---

按照以上步骤，您的科研课题管理系统应该能够在 Windows Server 2016 上成功运行，并且内网用户可以通过 IP 地址访问。如果遇到问题，请检查防火墙设置和网络配置。