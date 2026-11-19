# 在当前 PowerShell 会话里准备好这个项目的环境。
# 用法：  . .\scripts\dev.ps1
#
# 做三件事：
#   1. uv 的缓存与 Python 目录指向工作区内（沙箱/受限环境也能写）
#   2. PYTHONPATH 指向 src，方便直接 python -m / streamlit run
#   3. 控制台按 UTF-8 输出，避免中文路径乱码

$projectRoot = Split-Path -Parent $PSScriptRoot

$env:UV_CACHE_DIR = Join-Path $projectRoot '.cache\uv'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $projectRoot '.cache\python'
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
# 国内网络：官方 PyPI 很慢，默认走清华镜像（想换回来就删掉这行）
if (-not $env:UV_DEFAULT_INDEX) {
    $env:UV_DEFAULT_INDEX = 'https://pypi.tuna.tsinghua.edu.cn/simple'
}
$env:LITELLM_LOCAL_MODEL_COST_MAP = 'True'

Write-Host "project : $projectRoot"
Write-Host "python  : $projectRoot\.venv\Scripts\python.exe"
Write-Host "index   : $env:UV_DEFAULT_INDEX"
