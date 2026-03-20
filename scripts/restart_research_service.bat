@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "PS_SCRIPT=%SCRIPT_DIR%restart_research_service.ps1"

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo Failed with exit code %RC%.
)

endlocal & exit /b %RC%
