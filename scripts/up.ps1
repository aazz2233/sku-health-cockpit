#Requires -Version 5.1
<#
  一条命令把整套系统拉起来：
    1. 检查环境（uv / .venv / .env）
    2. 按需生成数据、入库、建指标、跑异常检测
    3. 启动 Streamlit 看板

  用法：
    .\scripts\up.ps1                # 缺什么补什么，然后开看板
    .\scripts\up.ps1 -Rebuild       # 强制重新生成数据 + 全量重跑
    .\scripts\up.ps1 -NoDetect      # 跳过异常检测与 AI 归因（快）
    .\scripts\up.ps1 -Port 8501     # 指定端口
#>

param(
    [switch]$Rebuild,
    [switch]$NoDetect,
    [switch]$WithAI,
    [int]$Port = 8501
)

$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $root 'src'
# keep uv's cache inside the project: works even when the user profile is not writable
if (-not $env:UV_CACHE_DIR) { $env:UV_CACHE_DIR = Join-Path $root '.cache\uv' }
if (-not $env:UV_PYTHON_INSTALL_DIR) { $env:UV_PYTHON_INSTALL_DIR = Join-Path $root '.cache\python' }
Set-Location $root

function Step([string]$text) { Write-Host "`n== $text" -ForegroundColor Cyan }

Step '检查环境'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv 未安装：https://docs.astral.sh/uv/'
}
if (-not (Test-Path '.env')) {
    throw '缺少 .env：先复制 .env.example 为 .env 并填写 MYSQL_DSN'
}
Write-Host "  项目目录: $root"

Step '同步依赖'
uv sync --quiet
if ($LASTEXITCODE -ne 0) { throw 'uv sync 失败' }

Step '确认数据库连接'
uv run python -c "from sku_cockpit.db import get_engine, ping; print('  ', ping(get_engine()))"
if ($LASTEXITCODE -ne 0) { throw '数据库连不上：检查 .env 里的 MYSQL_DSN' }

$hasData = $false
try {
    $count = uv run python -c "from sku_cockpit.db import get_engine; from sqlalchemy import text; print(get_engine().connect().execute(text('select count(*) from mart_sku_daily')).scalar())" 2>$null
    if ($LASTEXITCODE -eq 0 -and [int]$count -gt 0) { $hasData = $true }
} catch { $hasData = $false }

if ($Rebuild -or -not $hasData) {
    Step '生成演示数据'
    uv run python -m sku_cockpit generate
    if ($LASTEXITCODE -ne 0) { throw 'generate 失败' }

    Step '入库（全量重载）'
    uv run python -m sku_cockpit ingest --drop --report
    if ($LASTEXITCODE -ne 0) { throw 'ingest 失败' }

    Step '构建指标层'
    uv run python -m sku_cockpit metrics
    if ($LASTEXITCODE -ne 0) { throw 'metrics 失败' }
} else {
    Write-Host '  已有指标数据，跳过生成与入库（-Rebuild 可强制重来）'
}

if (-not $NoDetect) {
    Step '运行异常检测'
    uv run python -m sku_cockpit detect
    if ($LASTEXITCODE -ne 0) { throw 'detect 失败' }

    if ($WithAI) {
        Step 'AI 归因（前 50 条）'
        uv run python -m sku_cockpit diagnose --limit 50
    }
}

Step '生成日报'
uv run python -m sku_cockpit report-daily

Step "启动看板 http://localhost:$Port"
uv run streamlit run src/sku_cockpit/app/dashboard.py --server.port $Port --server.headless true
