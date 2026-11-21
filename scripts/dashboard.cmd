@echo off
rem Start only the dashboard (no data work). Bypasses the execution policy for
rem this child process only, keeps uv's cache inside the project, and runs
rem headless so Streamlit never blocks on its first-run email prompt.
setlocal
cd /d "%~dp0.."
set PYTHONPATH=%CD%\src
set PYTHONUTF8=1
set UV_CACHE_DIR=%CD%\.cache\uv
set UV_PYTHON_INSTALL_DIR=%CD%\.cache\python
uv run streamlit run src\sku_cockpit\app\dashboard.py --server.port 8501 --server.headless true --browser.gatherUsageStats false
if errorlevel 1 pause
