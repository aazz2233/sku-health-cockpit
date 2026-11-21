@echo off
rem One-click launcher for Windows. It bypasses the script execution policy
rem for its own child process only, so no system setting is changed.
setlocal
cd /d "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0up.ps1" %*
if errorlevel 1 (
    echo.
    echo [failed] see the messages above.
    pause
)
