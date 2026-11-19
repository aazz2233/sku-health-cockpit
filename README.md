# SKU Health Cockpit

> 多店铺 SKU 利润、库存健康与异常诊断 —— 跨境电商内部工具演示
> Multi-store SKU profit / inventory health / anomaly diagnosis cockpit for cross-border e-commerce.

把多个店铺、多种货币、表头各不相同的订单/退款/广告/库存表格，
统一成一套口径，算出每个 SKU 的真实利润与断货风险，识别异常，
并让 AI 只负责"解释原因、给建议"，不确定时转人工复核。

## 核心设计

**规则算数，AI 解释，人来拍板。**

- 财务数字由规则引擎计算：可复现、可审计、零成本、不产生幻觉
- AI 只回答"为什么可能异常、接下来做什么"，且必须引用已算好的指标
- 每个结论都带证据（指标 + 窗口 + 数值），可追溯到原始行

## 状态

🚧 开发中（P0 工程骨架）。功能与验收标准见 `实施方案.md`。

## 快速开始

```powershell
# 依赖（国内网络建议先设镜像源）
$env:UV_DEFAULT_INDEX = 'https://pypi.tuna.tsinghua.edu.cn/simple'
uv sync

# 检查
uv run ruff check src tests
uv run mypy
uv run pytest
```

环境说明（本机实测）见 [`docs/environment.md`](docs/environment.md)。

## 目录

```
src/sku_cockpit/   业务代码（数据层 → 指标层 → 异常识别 → AI 归因 → 应用）
tests/             单元测试与对账测试
scripts/           开发脚本（bootstrap / dev-env）
docs/              环境说明、数据字典、指标口径、规则说明
data/              生成器与样例数据（生成物不入库）
```

## 数据与合规

- 不接触任何真实经营数据；演示数据由带 seed 的生成器产生
- 不抓取需登录页面、不绕过反爬；采集器默认关闭
- 密钥只走 `.env`，仓库里只有 `.env.example`

## License

MIT
