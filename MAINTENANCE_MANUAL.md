# 科研课题管理系统 运维维护手册

## 一、系统概况

- **代码路径**：`D:\\gemini_rebuild`
- **Django 项目模块**：`project_manager`
- **管理脚本**：`D:\\gemini_rebuild\\django_service.py`
- **Windows 服务名**：`ResearchManagementService`
- **服务显示名称**：`Research Management Django Service`
- **监听端口**：`1027`
- **访问地址**：
  - 服务器本机：`http://127.0.0.1:1027`
  - 内网其它电脑：`http://<服务器IP>:1027`

---

## 二、如何启动 / 停止 / 重启服务

### 2.1 图形界面（services.msc）

1. 按 `Win + R`，输入 `services.msc` 回车。
2. 在服务列表中找到：**Research Management Django Service**。
3. 常用操作：
   - **启动**：右键 → “启动”。
   - **停止**：右键 → “停止”。
   - **重启**：右键 → “重新启动”。
   - **设置为开机自启**：右键 → “属性” → “启动类型” 选择 **自动** → “确定”。

> 建议：正式环境将启动类型设置为 **自动**，保证服务器重启后系统自动在后台运行。

### 2.2 命令行方式（管理员 CMD）

1. 以 **管理员身份** 打开 `cmd.exe`。
2. 进入项目目录：

```cmd
cd /d D:\gemini_rebuild
```

3. 常用命令：

- **启动服务**

```cmd
python django_service.py start
```

- **停止服务**

```cmd
python django_service.py stop
```

- **重启服务**

```cmd
python django_service.py restart
```

- **查看服务状态**

```cmd
sc query ResearchManagementService
```

- **设置为开机自启动**（推荐在 cmd 中执行）

```cmd
sc.exe config ResearchManagementService start= auto
```

---

## 三、更新代码时的操作流程

### 3.1 改了代码是不是会直接生效？

- 当前服务内部执行的是：

```python
manage.py runserver 0.0.0.0:1027
```

- `runserver` 带有自动重载功能：
  - 修改 `.py` 或模板文件后，通常会自动重启子进程，几秒内生效。
- 但在正式环境，**不建议依赖自动重载**：
  - 更新过程不可控，可能在用户访问过程中重启。

**推荐原则**：

> 生产环境的所有更新，都按照“停止服务 → 更新 → 启动服务 → 验证”的维护流程来做。


### 3.2 仅修改 Python 业务代码（不改模型、不加依赖）

典型场景：修改视图逻辑、修 Bug、轻量改动。

1. **停止服务**

```cmd
cd /d D:\gemini_rebuild
python django_service.py stop
```

2. **更新代码**

- 覆盖更新 `core`、`templates` 等代码文件，或
- 执行 `git pull` 拉取最新代码。

3. **（可选）快速检查配置**

```cmd
python manage.py check
```

4. **启动服务**

```cmd
python django_service.py start
```

5. **验证**

- 本机访问：`http://127.0.0.1:1027`
- 内网访问：`http://<服务器IP>:1027`

确保关键功能正常后，再通知用户更新完成。

---

### 3.3 修改数据库模型 / 新增依赖

典型场景：添加字段或新表、修改 `models.py`，或者在 `requirements.txt` 中新增第三方库。

1. **通知用户维护时间**，选择业务低峰期。
2. **停止服务**

```cmd
cd /d D:\gemini_rebuild
python django_service.py stop
```

3. **（强烈建议）先做一次备份**

```cmd
copy D:\gemini_rebuild\db.sqlite3 D:\backup\db_backup_YYYYMMDD.sqlite3
```

4. **更新代码和依赖**

- 更新项目代码（覆盖或 `git pull`）。
- 如有新依赖或版本变化：

```cmd
pip install -r requirements.txt --upgrade
```

5. **执行数据库迁移**

```cmd
python manage.py migrate
```

6. **重新收集静态文件（如有前端静态资源变更）**

```cmd
python manage.py collectstatic --noinput
```

7. **启动服务**

```cmd
python django_service.py start
```

8. **验证**

- 主页是否能打开。
- 登录是否正常。
- 新增/修改的功能是否工作正常。

---

### 3.4 仅修改前端静态文件 / 样式

典型场景：修改 CSS / JS / 图片等资源。

1. **停止服务**（建议，确保更新过程稳定）

```cmd
cd /d D:\gemini_rebuild
python django_service.py stop
```

2. **更新静态文件**（覆盖对应目录或通过构建工具生成）。

3. **重新收集静态文件**

```cmd
python manage.py collectstatic --noinput
```

4. **启动服务**

```cmd
python django_service.py start
```

5. 如用户页面样式没有立刻变化，提示刷新/清理浏览器缓存。

---

## 四、常用排查命令

在 `D:\\gemini_rebuild` 目录、管理员 CMD 中使用：

- **查看服务状态**

```cmd
sc query ResearchManagementService
```

- **检查 1027 端口是否在监听**

```cmd
netstat -an | findstr :1027
```

- **检查 Django 配置是否有问题**

```cmd
python manage.py check
python manage.py check --deploy
```

- **查看数据库迁移状态**

```cmd
python manage.py showmigrations
```

---

## 五、运维最佳实践

1. **所有更新有维护窗口**
   - 按“停止服务 → 更新 → 启动服务 → 验证”的流程，避免线上自动重载带来的不确定性。

2. **重要更新前务必备份**
   - 至少备份 `db.sqlite3` 和项目目录 `D:\\gemini_rebuild`。

3. **先在测试环境验证，再更新生产环境**
   - 使用相同或相近的数据验证功能和性能。

4. **定期检查运行状态**
   - 每周查看服务是否 `RUNNING`，端口是否在监听，系统是否能正常访问。

5. **出现异常优先查看**
   - 浏览器提示信息和 HTTP 状态码。
   - 服务状态（`sc query`）。
   - 必要时再看 Windows 事件日志。
