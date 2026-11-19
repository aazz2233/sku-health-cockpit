# 开发环境说明（P0 实测记录）

> 记录日期：2026-11-19 · Windows 10/11 · 账户 `Lenovo`（在管理员组，但进程未提权）
> 目的：让任何人（包括未来的你）知道"为什么这么配"，以及换机器时要验证什么

---

## 一、结论表

| 组件 | 状态 | 位置 / 版本 | 说明 |
| --- | --- | --- | --- |
| Git | ✅ 已装 | `D:\tools\PortableGit\cmd\git.exe` · 2.56.1 | 便携版，免安装；已加入**用户级** PATH（新开的终端生效） |
| Git 身份 | ✅ 已设 | `ljj <luojiajie@whut.edu.cn>` | 只写在**本仓库**的 `.git/config`，没动全局 |
| Python | ✅ 已装 | `.venv` · CPython 3.12.13 | 由 uv 管理，装在 `.cache\python\` |
| 依赖 | ✅ 已锁 | `uv.lock` · 118 个包 | pandas 3.1.1 / duckdb 2.0.0 / pydantic 2.14.3 / streamlit 1.67.0 / pymysql 1.2.4 |
| uv | ✅ 已有 | 0.11.28 | 依赖与 Python 版本管理 |
| VS Code | ✅ 已有 | `F:\Microsoft VS Code\` | 把解释器选成 `.venv\Scripts\python.exe` |
| MySQL | ⚠️ 服务可用 | 8.0.46 · 服务 `MySQL80` · `127.0.0.1:3306` | **凭据未拿到**：用 `root` + `password` 被拒（ERROR 1045），需要正确密码或重置 |
| Docker | ❌ 未装 | — | 本机没有 WSL2；账户是管理员，提权后可装（步骤见第五节） |
| Node.js | ✅ 已有 | `D:\node.js\node.exe` | 本项目用不到，备用 |
| PowerShell | ⚠️ 5.1 | 系统自带 | 没有 pwsh 7；脚本必须兼容 5.1，且**必须存为 UTF-8 with BOM**，否则中文注释会让解析器报错 |

---

## 二、四个必须知道的环境特性

### 1. 官方 PyPI 在这台机器上超时 → 用清华镜像

实测 `https://pypi.org/simple/` 25 秒只下 748KB，`uv sync` 三次重试后失败；锁文件里若记录的是 `files.pythonhosted.org` 地址也会超时。

```powershell
$env:UV_DEFAULT_INDEX = 'https://pypi.tuna.tsinghua.edu.cn/simple'
$env:UV_HTTP_TIMEOUT = '300'
uv sync
```

> `scripts/dev.ps1` 已经默认设好镜像。注意：`uv.lock` 里记录的下载地址跟随生成时的镜像源。

### 2. 沙箱内 HTTPS 握手失败（只影响自动化代理）

代理以受限令牌运行时，Windows TLS 凭据获取失败（`SEC_E_NO_CREDENTIALS`）：443 端口通，但握手立刻断。
**影响**：联网命令（`uv sync`、`git clone/push`、下载）要在放开权限下执行；本地命令（测试、看板、git status/commit）不受影响。
**你自己的终端没有这个问题。** 纯本地 TCP（如连本机 MySQL 3306）不受影响。

### 3. uv 缓存必须放在工作区内（仅代理侧）

默认缓存在 `%LOCALAPPDATA%\uv\cache`，沙箱写入被拒。统一用：

```powershell
$env:UV_CACHE_DIR = 'D:\实习项目demo\.cache\uv'
$env:UV_PYTHON_INSTALL_DIR = 'D:\实习项目demo\.cache\python'
```

`.cache/` 已在 `.gitignore` 中。

### 4. 受限执行下，删除/移动某些已存在文件会被拒

表现为 ACL 完全正常、但 `Move-Item` / `Remove-Item` 报"访问被拒绝"，甚至出现连 ACL 都读不了的残留目录（如 pytest 首次运行留下的 `pytest-cache-files-*`）。
**原因**：执行层的策略拒绝，与文件权限项无关。
**处理**：这类操作放到放开权限下做；`.gitignore` 已加入 `pytest-cache-files-*/` 兜底。

---

## 三、仓库状态

```
git init 完成，分支 main，首次提交已包含：14 个文件
```

包含：`pyproject.toml`、`uv.lock`、`.python-version`、`Makefile`、`.gitignore`、
`.pre-commit-config.yaml`、`.github/workflows/ci.yml`、`README.md`、`docs/environment.md`、
`scripts/bootstrap.ps1`、`scripts/dev.ps1`、`src/sku_cockpit/__init__.py`、`tests/test_smoke.py`、`.env.example`

**不进仓库**的现实：岗位截图与两份规划文档已挪到 `_notes/`（已 gitignore）；`.venv`、`.cache`、`data/generated`、`reports` 均被忽略。

首次自查结果：

| 检查 | 结果 |
| --- | --- |
| `ruff check src tests` | All checks passed |
| `ruff format --check` | 2 files already formatted |
| `mypy` | Success: no issues found |
| `pytest` | 2 passed |

---

## 四、日常用法

```powershell
. .\scripts\dev.ps1          # 设置镜像、缓存、PYTHONPATH
uv sync                      # 安装/更新依赖
uv run pytest                # 测试
uv run ruff check src tests  # 静态检查
uv run mypy                  # 类型检查
```

VS Code：解释器选 `D:\实习项目demo\.venv\Scripts\python.exe`。

---

## 五、Docker 怎么装（账户已有权限，只差提权）

当前进程 `IsInRole(Administrator)` 为 False，但账户在 `Administrators` 组、UAC 开启 —— 所以**不需要"申请权限"**，只需要用管理员身份运行一次。

```powershell
# 1) 管理员 PowerShell（开始菜单搜 PowerShell → 右键"以管理员身份运行"）
wsl --install --no-distribution     # 装 WSL2，需重启

# 2) 重启后，管理员 PowerShell
wsl --update
winget install -e --id Docker.DockerDesktop
# 3) 再重启，启动 Docker Desktop，等托盘图标变绿
docker run --rm hello-world
```

注意：
- 安装 WSL2 和 Docker Desktop **各需一次重启**，建议别在写代码的中间做
- 虚拟化需在 BIOS 开启（`HypervisorPresent` 查询被系统拒绝，装 WSL 时报错会直接提示）
- 装好前，P5 的容器化交付物先写好、不在本地验证

---

## 六、版本注意点（写代码前先记住）

| 依赖 | 版本 | 注意 |
| --- | --- | --- |
| pandas | **3.1.1** | 已是 3.x：Copy-on-Write 默认开启，字符串列默认 `str` dtype，`inplace` 行为收紧 |
| numpy | 2.5.5 | 2.x，`np.float_` 等旧别名已移除 |
| duckdb | 2.0.0 | `read_csv` 参数有调整 |
| pydantic | 2.14.3 | v2 写法（`model_validate`） |
| pandera | 0.36.0 | `pa.DataFrameSchema` + `pa.checks` |
| pytest | 9.2.0 | `--strict-markers` 已开 |
| litellm | 1.110.0 | import 时默认去 GitHub 拉价目表，设 `LITELLM_LOCAL_MODEL_COST_MAP=True` |

---

## 七、待决事项

1. **MySQL 凭据**：`root`/`password` 被拒。需要：正确的 root 密码，或授权我用 `--skip-grant-tables` 重置（要短暂停服务、需要管理员），或改用其他已有账号
2. **Docker 安装时机**：现在装（两次重启）还是等 P5 前

---

## 八、回滚方式

- 删 Git：删 `D:\tools\PortableGit`，并从用户 PATH 移除 `D:\tools\PortableGit\cmd`
- 删 Python 与依赖：删 `.venv`、`.cache\python`、`.cache\uv`
- 权限修复备份：`D:\实习项目demo-acl-recovery\`（`.json` + `.ps1` 恢复脚本）
