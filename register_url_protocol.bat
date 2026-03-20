@echo off
chcp 65001 >nul

REM Server-side protocol handler for cscec-pm://open-folder/<project-folder>
REM Update this path if your server projects root is different
set "PROJECTS_BASE_PATH=D:\gemini_rebuild\projects"

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

set "FOLDER_PATH=%PROJECTS_BASE_PATH%\!DECODED_PROJECT_ID!"
if not exist "!FOLDER_PATH!" (
  echo Error: Project folder not found: !FOLDER_PATH!
  exit /b 1
)

echo Opening: !FOLDER_PATH!
explorer.exe "!FOLDER_PATH!"

endlocal
exit /b 0
