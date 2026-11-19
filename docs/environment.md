# 开发环境说明（P0 实测记录）

> 记录日期：2026-11-19 · Windows 10/11 · 无管理员权限
> 目的：让任何人（包括未来的你）知道"为什么这么配"，以及换机器时要验证什么

---

## 一、结论表

| 组件 | 状态 | 位置 / 版本 | 说明 |
| --- | --- | --- | --- |
| Git | ✅ 已装 | `D:\tools\PortableGit\cmd\git.exe` · 2.56.1 | 便携版，免管理员；已加入**用户级** PATH |
| Python | ✅ 已装 | `.venv` · CPython 3.12.13 | 由 uv 管理，装在 `.cache\python\` |
| 依赖 | ✅ 已装 | `uv.lock`（114 包） | pandas 3.1.1 / duckdb 2.0.0 / pydantic 2.14.3 / streamlit 1.67.0 |
| uv | ✅ 已有 | 0.11.28 | 依赖与 Python 版本管理 |
| VS Code | ✅ 已有 | `F:\Microsoft VS Code\` | 装了 Python 扩展即可调试 |
| MySQL | ✅ 已有 | 8.0.46 · 服务 `MySQL80` · `127.0.0.1:3306` | 客户端在 `C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe` |
| Node.js | ✅ 已有 | `D:\node.js\node.exe` | 本项目用不到，备用 |
| Docker | ❌ 不可用 | — | 未安装，且本机无管理员、无 WSL2，无法在本机跑容器 |
| PowerShell | ⚠️ 5.1 | 系统自带 | 没有 pwsh 7；脚本必须兼容 5.1（不能用 `??`、`-Parallel`） |

---

## 二、三个必须知道的环境特性

### 1. 官方 PyPI 在这台机器上超时 → 用清华镜像

实测 `https://pypi.org/simple/` 25 秒只下 748KB，`uv sync` 直接 3 次重试后失败。
**解决**：`UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple`，并把 `UV_HTTP_TIMEOUT` 调到 300。

```powershell
$env:UV_DEFAULT_INDEX = 'https://pypi.tuna.tsinghua.edu.cn/simple'
$env:UV_HTTP_TIMEOUT = '300'
uv sync
```

### 2. 沙箱内 HTTPS 握手失败（仅影响自动化代理，不影响你自己的终端）

代理以受限令牌运行时，Windows 的 TLS 凭据获取失败（`SEC_E_NO_CREDENTIALS`），表现为"能连通 443 端口，但握手立刻断"。
**影响**：凡是需要联网的命令（`uv sync`、`git clone/push`、下载）都要在放开权限下执行；纯本地命令（跑测试、看板、git status）不受影响。
**你自己的 PowerShell / VS Code 终端没有这个问题**，正常联网。

### 3. uv 缓存必须放在工作区内（仅代理侧）

默认缓存在 `%LOCALAPPDATA%\uv\cache`，沙箱写入会被拒。代理侧统一用：

```powershell
$env:UV_CACHE_DIR = 'D:\实习项目demo\.cache\uv'
$env:UV_PYTHON_INSTALL_DIR = 'D:\实习项目demo\.cache\python'
```

`.cache/` 已在 `.gitignore` 中，不会进仓库。

---

## 三、日常用法

```powershell
# 依赖（首次或改了 pyproject.toml）
uv sync

# 跑测试 / 代码检查
uv run pytest
uv run ruff check .
uv run mypy src

# 直接用虚拟环境里的解释器
.\.venv\Scripts\python.exe -c "import pandas; print(pandas.__version__)"
```

> 建议在 VS Code 里把解释器选为 `D:\实习项目demo\.venv\Scripts\python.exe`。

---

## 四、版本注意点（写代码前先记住）

| 依赖 | 版本 | 注意 |
| --- | --- | --- |
| pandas | **3.1.1** | 已是 3.x：Copy-on-Write 默认开启，`inplace=True` 行为收紧，字符串列默认 `str` dtype —— 不写 2.x 时代的旧写法 |
| numpy | 2.5.5 | 2.x，`np.float_` 等旧别名已移除 |
| duckdb | 2.0.0 | SQL 语法变化不大，但 `read_csv` 参数有调整 |
| pydantic | 2.14.3 | v2 写法（`model_validate`，不是 `parse_obj`） |
| pandera | 0.36.0 | 0.20+ 的 API：`pa.DataFrameSchema` + `pa.checks` |
| pytest | 9.2.0 | `--strict-markers` 已开，新 marker 要注册 |
| litellm | 1.110.0 | import 时会去 GitHub 拉价目表，设 `LITELLM_LOCAL_MODEL_COST_MAP=True` 避免 3 次超时 |

---

## 五、待决事项

1. **Docker**：本机跑不了（无管理员 + 无 WSL2）。三个选项：
   - a) 先不装：P5 提供 `Dockerfile` + `compose.yaml`，只在 README 声明"在服务器/有 WSL2 的机器上验证"
   - b) 申请管理员权限后装 Docker Desktop（需重启，约 30 分钟）
   - c) 改用 `.venv` 直接跑，Docker 只作为交付物不本地验证
2. **MySQL**：本机 8.0 服务可用。要用它做"可选存储 profile"的话，需要一个能建库的账号（用户名/密码），或者确认用 `root` 并自行提供密码。
3. **MySQL Workbench** 已装（`C:\Program Files\MySQL\MySQL Workbench 8.0 CE`），看数据方便。

---

## 六、回滚方式

- 删 Git 便携版：删掉 `D:\tools\PortableGit`，再把 `D:\tools\PortableGit\cmd` 从用户 PATH 移除
- 删 Python 与依赖：删掉 `.venv`、`.cache\python`、`.cache\uv`
- 本机权限修复的备份在 `D:\实习项目demo-acl-recovery\`（`.json` + `.ps1` 恢复脚本）
