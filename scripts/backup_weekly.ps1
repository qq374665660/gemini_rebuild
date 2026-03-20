param(
    [string]$DestinationRoot = "\\192.168.0.50\\d\\keti-backup-projects",
    [int]$KeepLast = 0,
    [string]$LogRoot = ""
)

$ErrorActionPreference = "Stop"

try {
    (Get-Process -Id $PID).PriorityClass = "BelowNormal"
} catch {
    # If we cannot change priority, continue without failing.
}

function Write-Log {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$timestamp] $Message"
    Write-Output $line
    if ($script:LogFile) {
        Add-Content -Path $script:LogFile -Value $line
    }
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$projectsPath = Join-Path $repoRoot "projects"
$dbPath = Join-Path $repoRoot "db.sqlite3"

if (-not (Test-Path $projectsPath)) {
    throw "Projects path not found: $projectsPath"
}
if (-not (Test-Path $dbPath)) {
    throw "Database file not found: $dbPath"
}

if (-not $LogRoot) {
    $LogRoot = Join-Path $DestinationRoot "logs"
}
New-Item -ItemType Directory -Path $DestinationRoot -Force | Out-Null
New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$backupRoot = Join-Path $DestinationRoot $timestamp
$backupProjects = Join-Path $backupRoot "projects"
$backupDb = Join-Path $backupRoot "db.sqlite3"

New-Item -ItemType Directory -Path $backupProjects -Force | Out-Null
$script:LogFile = Join-Path $LogRoot ("backup_{0}.log" -f $timestamp)

try {
    Write-Log "Backup started."
    Write-Log "Source projects: $projectsPath"
    Write-Log "Source db: $dbPath"
    Write-Log "Destination: $backupRoot"

    Write-Log "Copying projects with robocopy..."
    $robocopyArgs = @(
        $projectsPath,
        $backupProjects,
        "/MIR",
        "/R:2",
        "/W:2",
        "/XJ",
        "/FFT",
        "/IPG:10",
        "/COPY:DAT",
        "/NP"
    )
    $robocopyResult = & robocopy @robocopyArgs
    $robocopyExit = $LASTEXITCODE
    Write-Log "Robocopy exit code: $robocopyExit"
    if ($robocopyExit -ge 8) {
        throw "Robocopy failed with exit code $robocopyExit"
    }

    Write-Log "Backing up sqlite database..."
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCmd) {
        $pythonCmd = Get-Command py -ErrorAction SilentlyContinue
    }

    if ($pythonCmd) {
        $code = @"
import os
import sqlite3
src = r'''$dbPath'''
dst = r'''$backupDb'''
os.makedirs(os.path.dirname(dst), exist_ok=True)
con = sqlite3.connect(src)
dest = sqlite3.connect(dst)
con.backup(dest)
dest.close()
con.close()
"@
        & $pythonCmd.Source -c $code
        Write-Log "Database backup completed via sqlite backup API."
    } else {
        Copy-Item -Path $dbPath -Destination $backupDb -Force
        Write-Log "Database backup completed via file copy (python not found)."
    }

    if ($KeepLast -gt 0) {
        Write-Log "Applying retention policy: keep last $KeepLast backups."
        $backupDirs = Get-ChildItem -Path $DestinationRoot -Directory |
            Where-Object { $_.Name -match '^\d{8}_\d{6}$' } |
            Sort-Object Name -Descending
        if ($backupDirs.Count -gt $KeepLast) {
            $toRemove = $backupDirs | Select-Object -Skip $KeepLast
            foreach ($dir in $toRemove) {
                Write-Log "Removing old backup: $($dir.FullName)"
                Remove-Item -Path $dir.FullName -Recurse -Force
            }
        }
    }

    Write-Log "Backup finished."
} catch {
    Write-Log "Backup failed: $($_.Exception.Message)"
    throw
}
