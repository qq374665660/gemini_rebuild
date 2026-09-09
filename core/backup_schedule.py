"""Read and update the Windows scheduled task used for automatic backups."""

import json
import logging
import os
import re
import subprocess
from pathlib import PureWindowsPath

from django.conf import settings


logger = logging.getLogger(__name__)


class BackupScheduleError(RuntimeError):
    """Raised when the backup schedule cannot be read or updated safely."""


MIN_INTERVAL_DAYS = 1
MAX_INTERVAL_DAYS = 3650
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _powershell_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _task_parts():
    configured_name = str(getattr(settings, "BACKUP_TASK_NAME", r"\KetiBackupWeekly")).strip()
    if not configured_name:
        raise BackupScheduleError("未配置自动备份计划任务名称。")

    normalized = str(PureWindowsPath(configured_name))
    if not normalized.startswith("\\"):
        normalized = "\\" + normalized
    task_path, _, task_name = normalized.rpartition("\\")
    if not task_name:
        raise BackupScheduleError("自动备份计划任务名称无效。")
    task_path = (task_path or "\\").rstrip("\\") + "\\"
    return task_name, task_path


def _run_powershell(script):
    if os.name != "nt":
        raise BackupScheduleError("自动备份时间管理仅支持 Windows 服务器。")

    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupScheduleError(f"无法连接 Windows 计划任务服务：{exc}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "未知错误").strip()
        raise BackupScheduleError(f"计划任务操作失败：{detail}")
    return completed.stdout.strip()


def _read_task_payload():
    task_name, task_path = _task_parts()
    script = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$task = Get-ScheduledTask -TaskName {_powershell_literal(task_name)} -TaskPath {_powershell_literal(task_path)}
$info = Get-ScheduledTaskInfo -TaskName {_powershell_literal(task_name)} -TaskPath {_powershell_literal(task_path)}
$trigger = $task.Triggers | Where-Object {{ $_.CimClass.CimClassName -eq 'MSFT_TaskDailyTrigger' }} | Select-Object -First 1
$triggerType = 'Daily'
if ($trigger) {{
    $intervalDays = [int]$trigger.DaysInterval
}} else {{
    $trigger = $task.Triggers | Where-Object {{ $_.CimClass.CimClassName -eq 'MSFT_TaskWeeklyTrigger' }} | Select-Object -First 1
    if (-not $trigger) {{ throw '未找到按天或按周执行的备份触发器。' }}
    $daysMask = [int]$trigger.DaysOfWeek
    $selectedDayCount = 0
    foreach ($bit in @(1, 2, 4, 8, 16, 32, 64)) {{
        if (($daysMask -band $bit) -ne 0) {{ $selectedDayCount++ }}
    }}
    if ($selectedDayCount -ne 1) {{ throw '原每周任务包含多个执行日，无法换算为固定天数间隔。' }}
    $triggerType = 'Weekly'
    $intervalDays = [int]$trigger.WeeksInterval * 7
}}
$startAt = [DateTimeOffset]::Parse([string]$trigger.StartBoundary).ToLocalTime()
[pscustomobject]@{{
    task_name = $task.TaskName
    state = [string]$task.State
    enabled = [bool]$task.Settings.Enabled
    start_boundary = [string]$trigger.StartBoundary
    time = $startAt.ToString('HH:mm')
    trigger_type = $triggerType
    interval_days = $intervalDays
    last_run_time = if ($info.LastRunTime -and $info.LastRunTime.Year -gt 1900) {{ $info.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss') }} else {{ '' }}
    next_run_time = if ($info.NextRunTime -and $info.NextRunTime.Year -gt 1900) {{ $info.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss') }} else {{ '' }}
    last_result = [int]$info.LastTaskResult
}} | ConvertTo-Json -Compress
"""
    output = _run_powershell(script)
    json_line = next((line for line in reversed(output.splitlines()) if line.lstrip().startswith("{")), "")
    if not json_line:
        raise BackupScheduleError("计划任务未返回可识别的状态。")
    try:
        return json.loads(json_line)
    except json.JSONDecodeError as exc:
        raise BackupScheduleError("无法解析计划任务状态。") from exc


def get_backup_schedule():
    """Return a template-friendly representation of the configured task."""
    read_only = bool(getattr(settings, "BACKUP_SCHEDULE_READ_ONLY", False))
    if read_only:
        try:
            preview_interval = int(getattr(settings, "BACKUP_SCHEDULE_PREVIEW_INTERVAL_DAYS", 7))
        except (TypeError, ValueError):
            preview_interval = 7
        preview_time = str(getattr(settings, "BACKUP_SCHEDULE_PREVIEW_TIME", "02:00"))
        if not MIN_INTERVAL_DAYS <= preview_interval <= MAX_INTERVAL_DAYS:
            preview_interval = 7
        if not TIME_PATTERN.fullmatch(preview_time):
            preview_time = "02:00"
        return {
            "available": True,
            "read_only": True,
            "preview": True,
            "task_name": _task_parts()[0],
            "state": "Preview",
            "enabled": False,
            "trigger_type": "Preview",
            "interval_days": preview_interval,
            "time": preview_time,
            "last_run_time": "",
            "next_run_time": "",
            "last_result": -1,
            "last_result_success": False,
        }
    try:
        payload = _read_task_payload()
        try:
            interval_days = int(payload.get("interval_days", 0))
        except (TypeError, ValueError) as exc:
            raise BackupScheduleError("计划任务未返回有效的备份间隔。") from exc
        if not MIN_INTERVAL_DAYS <= interval_days <= MAX_INTERVAL_DAYS:
            raise BackupScheduleError("计划任务的备份间隔超出允许范围。")
        start_boundary = str(payload.get("start_boundary", ""))
        time_value = str(payload.get("time", ""))
        if not TIME_PATTERN.fullmatch(time_value):
            time_value = start_boundary[11:16] if len(start_boundary) >= 16 else ""
        last_result = int(payload.get("last_result", -1))
        return {
            "available": True,
            "read_only": read_only,
            "task_name": payload.get("task_name", ""),
            "state": payload.get("state", ""),
            "enabled": bool(payload.get("enabled", False)),
            "trigger_type": payload.get("trigger_type", ""),
            "interval_days": interval_days,
            "time": time_value,
            "last_run_time": payload.get("last_run_time", ""),
            "next_run_time": payload.get("next_run_time", ""),
            "last_result": last_result,
            "last_result_success": last_result == 0,
        }
    except BackupScheduleError as exc:
        logger.warning("Failed to read backup schedule: %s", exc)
        return {
            "available": False,
            "read_only": read_only,
            "error": "无法读取 Windows 自动备份计划任务，请确认任务名称及服务账户权限。",
        }


def update_backup_schedule(interval_days, time_value, enabled=True):
    """Update the daily trigger and whether the existing backup task is enabled."""
    if bool(getattr(settings, "BACKUP_SCHEDULE_READ_ONLY", False)):
        raise BackupScheduleError("开发环境为只读模式，不能修改正式自动备份时间。")
    try:
        interval_days = int(str(interval_days).strip())
    except (TypeError, ValueError) as exc:
        raise BackupScheduleError("备份间隔必须是整数天数。") from exc
    if not MIN_INTERVAL_DAYS <= interval_days <= MAX_INTERVAL_DAYS:
        raise BackupScheduleError(
            f"备份间隔必须在 {MIN_INTERVAL_DAYS} 到 {MAX_INTERVAL_DAYS} 天之间。"
        )
    if not TIME_PATTERN.fullmatch(time_value or ""):
        raise BackupScheduleError("请输入有效的备份时间，格式为 HH:MM。")
    enabled = str(enabled).strip().lower() in {"1", "true", "yes", "on"}

    task_name, task_path = _task_parts()
    hour, minute = (int(part) for part in time_value.split(":"))
    script = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$task = Get-ScheduledTask -TaskName {_powershell_literal(task_name)} -TaskPath {_powershell_literal(task_path)}
if (-not $task) {{ throw '未找到自动备份计划任务。' }}
$at = [DateTime]::Today.AddHours({hour}).AddMinutes({minute})
$trigger = New-ScheduledTaskTrigger -Daily -DaysInterval {interval_days} -At $at
Set-ScheduledTask -TaskName {_powershell_literal(task_name)} -TaskPath {_powershell_literal(task_path)} -Trigger $trigger | Out-Null
if ({'$true' if enabled else '$false'}) {{
    Enable-ScheduledTask -TaskName {_powershell_literal(task_name)} -TaskPath {_powershell_literal(task_path)} | Out-Null
}} else {{
    Disable-ScheduledTask -TaskName {_powershell_literal(task_name)} -TaskPath {_powershell_literal(task_path)} | Out-Null
}}
"""
    _run_powershell(script)
    return get_backup_schedule()
