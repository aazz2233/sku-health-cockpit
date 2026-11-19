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
| MySQL | ✅ 可用 | 8.0.46 · 服务 `MySQL80` · `127.0.0.1:3306` | root 密码已重置；应用账号 `cockpit` 仅有 `sku_cockpit` 库权限；连接串在 `.env` |
| Docker | ⏸ 不装 | — | 已决定不装：改为脚本化交付（第五节），Dockerfile 仅作可选附料 |
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

## 五、交付方式：不依赖 Docker 的"一键跑起来"

本机不装 Docker（无 WSL2，且两次重启会打断开发）。替代方案按优先级：

| 层级 | 做法 | 效果 |
| --- | --- | --- |
| 1. 脚本化启动 | `scripts/up.ps1`：检查环境 → `uv sync` → 生成演示数据 → 建表入库 → 启动看板 | 一条命令，10 分钟内看到界面 |
| 2. 固定依赖 | `uv.lock` + `.python-version` | 换机器重建环境可复现 |
| 3. 跨平台证明 | GitHub Actions 在 Ubuntu 上跑 lint/type/test | 证明"不是只在我电脑上能跑" |
| 4. Docker（可选附料） | `Dockerfile` + `compose.yaml` 写进仓库，README 标注 "not verified on this machine, provided as a reference" | 面试官若想容器化，有现成的，且不夸口 |

**为什么这么做**：面试官看的是"能不能跑起来 + 有没有交付意识"，不是"有没有用 Docker"。一条 `uv sync && uv run streamlit run ...` 的 README，比一个跑不通的 compose 文件更有说服力。诚实标注"未在本机验证"反而是加分项。

如果之后想补 Docker：账户已在管理员组，`wsl --install --no-distribution` → 重启 → `winget install Docker.DockerDesktop` → 重启即可。

---

## 六、MySQL 凭据与维护

- 应用账号：`cockpit`（`localhost` 与 `127.0.0.1` 各一条），**仅对 `sku_cockpit` 库有权限**
- 数据库：`sku_cockpit`，`utf8mb4` / `utf8mb4_0900_ai_ci`
- 连接串写在 `.env`（已 gitignore）；完整凭据与重置过程记在 `_notes/mysql-credentials.txt`
- root 密码已重置为强密码，仅用于维护，不进任何代码
- 已验证：SQLAlchemy 连接、DDL 往返、字符集均为 utf8mb4

**踩过的坑**（值得记住）：`mysqld --defaults-file="C:\ProgramData\MySQL\MySQL Server 8.0\my.ini"` 在 PowerShell 里用 `Start-Process -ArgumentList` 传参时，路径含空格必须显式加引号，否则被拆成两段，mysqld 报 `Failed to open required defaults file` 秒退；重置完还要记得把服务拉起来。

---

## 七、版本注意点（写代码前先记住）

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

## 八、待决事项

1. **推到 GitHub**：仓库还只在本地；需要确认 GitHub 账号、仓库名与可见性，并用 `gh` 或 HTTPS 凭据推送（首次推送要联网授权）
2. **pre-commit 钩子**：配置已就位，但 `pre-commit install` 要联网拉取 hook 仓库
3. **LLM Key**：目前 `LLM_PROVIDER=mock`，AI 归因层先用回放模式做；有真 key 再切

---

## 九、回滚方式

- 删 Git：删 `D:\tools\PortableGit`，并从用户 PATH 移除 `D:\tools\PortableGit\cmd`
- 删 Python 与依赖：删 `.venv`、`.cache\python`、`.cache\uv`
- 权限修复备份：`D:\实习项目demo-acl-recovery\`（`.json` + `.ps1` 恢复脚本）
