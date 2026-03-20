@echo off
chcp 65001 >nul

REM Client protocol handler for cscec-pm://open-folder/<project-folder>
REM Update this path to your network share root
set "NETWORK_SHARE_PATH=\\192.168.0.50\projects"

set "URL=%~1"
if "%URL%"=="" (
  echo Error: No URL provided.
  exit /b 1
)

set "URL=%URL:"=%"

for /f "tokens=2 delims=:" %%a in ("%URL%") do set "PROTOCOL_PART=%%a"
set "PROTOCOL_PART=%PROTOCOL_PART://=%"

for /f "tokens=1,2 delims=/" %%a in ("%PROTOCOL_PART%") do (
  set "ACTION=%%a"
  set "PROJECT_ID=%%b"
)

setlocal EnableDelayedExpansion
set "DECODED_PROJECT_ID=!PROJECT_ID!"
for /f "delims=" %%i in ('powershell -NoProfile -Command "Add-Type -AssemblyName System.Web; [System.Web.HttpUtility]::UrlDecode('!PROJECT_ID!')"') do set "DECODED_PROJECT_ID=%%i"

if /i not "!ACTION!"=="open-folder" (
  echo Unsupported operation: !ACTION!
  exit /b 1
)

if "!DECODED_PROJECT_ID!"=="" (
  echo Error: No project id provided.
  exit /b 1
)

set "FOLDER_PATH=%NETWORK_SHARE_PATH%\!DECODED_PROJECT_ID!"
echo Opening: !FOLDER_PATH!
explorer.exe "!FOLDER_PATH!"

endlocal
exit /b 0
