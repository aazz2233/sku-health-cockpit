#Requires -Version 5.1
<#
  P0 bootstrap: provision the local toolchain for sku-health-cockpit.

  What it does (idempotent, safe to re-run):
    1. Installs PortableGit (no admin rights needed) to D:\tools\PortableGit
       and adds its cmd\ to the *user* PATH.
    2. Points uv's cache and managed-Python dir inside the workspace
       (.cache\), because the sandbox can only write here.
    3. Installs CPython 3.12 via uv and syncs project dependencies.
    4. Verifies imports.

  This script needs network access, so it must run with full access.
#>

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$root = Split-Path -Parent $PSScriptRoot
Write-Host "== project root: $root"

# ---------------------------------------------------------------- git
$gitCmd = 'D:\tools\PortableGit\cmd\git.exe'
if (-not (Test-Path $gitCmd)) {
    Write-Host '== [1/4] installing PortableGit ...'
    $tools = 'D:\tools'
    New-Item -ItemType Directory -Force -Path $tools | Out-Null

    $api = Invoke-RestMethod -Uri 'https://api.github.com/repos/git-for-windows/git/releases/latest' `
        -Headers @{ 'User-Agent' = 'sku-health-cockpit-bootstrap' } -TimeoutSec 60
    $asset = $api.assets |
        Where-Object { $_.name -like 'PortableGit-*64-bit.7z.exe' } |
        Select-Object -First 1
    if (-not $asset) { throw 'PortableGit asset not found in latest release' }

    $pkg = Join-Path $env:TEMP $asset.name
    Write-Host "   downloading $($asset.name)"
    & curl.exe -L --fail --retry 3 --max-time 900 -o $pkg $asset.browser_download_url
    if ($LASTEXITCODE -ne 0) { throw "download failed with exit code $LASTEXITCODE" }

    Write-Host '   extracting'
    $p = Start-Process -FilePath $pkg -ArgumentList @("-o`"$tools\PortableGit`"", '-y') -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "extract failed with exit code $($p.ExitCode)" }
    Remove-Item $pkg -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path $gitCmd)) { throw "git not found at $gitCmd after extraction" }
}

$gitBin = Split-Path -Parent $gitCmd
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath -notlike "*$gitBin*") {
    [Environment]::SetEnvironmentVariable('Path', "$userPath;$gitBin", 'User')
    Write-Host "== git cmd added to user PATH: $gitBin"
}
if (($env:Path -split ';') -notcontains $gitBin) { $env:Path = "$gitBin;$env:Path" }
Write-Host ("== git: " + (& $gitCmd --version))

# ---------------------------------------------------------- uv paths
Write-Host '== [2/4] configuring uv cache inside workspace'
$env:UV_CACHE_DIR = Join-Path $root '.cache\uv'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $root '.cache\python'
New-Item -ItemType Directory -Force -Path $env:UV_CACHE_DIR | Out-Null
New-Item -ItemType Directory -Force -Path $env:UV_PYTHON_INSTALL_DIR | Out-Null
Write-Host "   UV_CACHE_DIR=$env:UV_CACHE_DIR"
Write-Host "   UV_PYTHON_INSTALL_DIR=$env:UV_PYTHON_INSTALL_DIR"

# pypi.org is very slow from mainland China; default to the Tsinghua mirror
# unless the caller already picked an index.
$env:UV_HTTP_TIMEOUT = '300'
$env:UV_HTTP_RETRIES = '5'
if (-not $env:UV_DEFAULT_INDEX) {
    $env:UV_DEFAULT_INDEX = 'https://pypi.tuna.tsinghua.edu.cn/simple'
}
Write-Host "   index: $env:UV_DEFAULT_INDEX"

# ------------------------------------------------------------- python
Write-Host '== [3/4] installing CPython 3.12 and syncing dependencies'
Push-Location $root
try {
    & uv python install 3.12
    if ($LASTEXITCODE -ne 0) { throw "uv python install failed ($LASTEXITCODE)" }

    & uv sync --python 3.12
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed ($LASTEXITCODE)" }
}
finally {
    Pop-Location
}

# ------------------------------------------------------------- verify
Write-Host '== [4/4] verifying'
Push-Location $root
try {
    & uv run python -c "import sys, pandas, numpy, duckdb, pydantic, pandera, openpyxl, yaml, jinja2, rapidfuzz, faker, streamlit, plotly, litellm, tenacity; print('python', sys.version.split()[0]); print('pandas', pandas.__version__); print('duckdb', duckdb.__version__); print('pydantic', pydantic.VERSION); print('streamlit', streamlit.__version__)"
    if ($LASTEXITCODE -ne 0) { throw "import check failed ($LASTEXITCODE)" }

    & uv run ruff --version
    & uv run mypy --version
    & uv run pytest --version
}
finally {
    Pop-Location
}

Write-Host ''
Write-Host '== bootstrap finished'
Write-Host "   git : $gitCmd  (new terminals pick it up from user PATH)"
Write-Host "   py  : $root\.venv\Scripts\python.exe"
Write-Host '   next: git init && git add -A && git commit -m "chore: bootstrap project"'
