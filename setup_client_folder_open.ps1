param(
    [string]$NetworkSharePath = "\\192.168.0.50\projects"
)

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "==== Folder Open Client Setup ====" -ForegroundColor Cyan
Write-Host "Network share path: $NetworkSharePath"

if (-not (Test-Path $NetworkSharePath)) {
    Write-Warning "Cannot access network share now: $NetworkSharePath"
    Write-Warning "Please verify network/share permissions. Protocol will still be registered."
}

$installDir = Join-Path $env:SystemDrive "cscec-pm"
$handlerPath = Join-Path $installDir "client_protocol_handler.bat"

New-Item -Path $installDir -ItemType Directory -Force | Out-Null

$handlerLines = @(
    '@echo off',
    'chcp 65001 >nul',
    '',
    'REM Client protocol handler for cscec-pm://open-folder/<project-folder>',
    'set "NETWORK_SHARE_PATH=__NETWORK_SHARE_PATH__"',
    '',
    'set "URL=%~1"',
    'if "%URL%"=="" (',
    '  echo Error: No URL provided.',
    '  exit /b 1',
    ')',
    '',
    'set "URL=%URL:"=%"',
    '',
    'for /f "tokens=2 delims=:" %%a in ("%URL%") do set "PROTOCOL_PART=%%a"',
    'set "PROTOCOL_PART=%PROTOCOL_PART://=%"',
    '',
    'for /f "tokens=1,2 delims=/" %%a in ("%PROTOCOL_PART%") do (',
    '  set "ACTION=%%a"',
    '  set "PROJECT_ID=%%b"',
    ')',
    '',
    'setlocal EnableDelayedExpansion',
    'set "DECODED_PROJECT_ID=!PROJECT_ID!"',
    'for /f "delims=" %%i in (''powershell -NoProfile -Command "Add-Type -AssemblyName System.Web; [System.Web.HttpUtility]::UrlDecode(''!PROJECT_ID!'')"'' ) do set "DECODED_PROJECT_ID=%%i"',
    '',
    'if /i not "!ACTION!"=="open-folder" (',
    '  echo Unsupported operation: !ACTION!',
    '  exit /b 1',
    ')',
    '',
    'if "!DECODED_PROJECT_ID!"=="" (',
    '  echo Error: No project id provided.',
    '  exit /b 1',
    ')',
    '',
    'set "FOLDER_PATH=%NETWORK_SHARE_PATH%\!DECODED_PROJECT_ID!"',
    'if not exist "!FOLDER_PATH!" (',
    '  echo Error: Project folder not found: !FOLDER_PATH!',
    '  exit /b 1',
    ')',
    '',
    'echo Opening: !FOLDER_PATH!',
    'explorer.exe "!FOLDER_PATH!"',
    '',
    'endlocal',
    'exit /b 0'
)

$handlerContent = (($handlerLines -join "`r`n").Replace('__NETWORK_SHARE_PATH__', $NetworkSharePath)) + "`r`n"
Set-Content -Path $handlerPath -Value $handlerContent -Encoding ASCII -Force

$baseKey = "HKCU:\Software\Classes\cscec-pm"
$commandKey = Join-Path $baseKey "shell\open\command"
$commandValue = "`"$handlerPath`" `"%1`""

New-Item -Path $baseKey -Force | Out-Null
New-ItemProperty -Path $baseKey -Name "(default)" -Value "URL:CSCEC Project-Manager Protocol" -PropertyType String -Force | Out-Null
New-ItemProperty -Path $baseKey -Name "URL Protocol" -Value "" -PropertyType String -Force | Out-Null
New-Item -Path (Join-Path $baseKey "shell") -Force | Out-Null
New-Item -Path (Join-Path $baseKey "shell\open") -Force | Out-Null
New-Item -Path $commandKey -Force | Out-Null
New-ItemProperty -Path $commandKey -Name "(default)" -Value $commandValue -PropertyType String -Force | Out-Null

Write-Host ""
Write-Host "Setup completed." -ForegroundColor Green
Write-Host "Handler path: $handlerPath"
Write-Host "Protocol command: $commandValue"
Write-Host ""
Write-Host "Next: restart browser, then click [打开项目文件夹] in the system."
