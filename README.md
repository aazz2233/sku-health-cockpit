# SKU Health Cockpit

> 多店铺 SKU 利润、库存健康与异常诊断 —— 跨境电商内部工具演示
> Multi-store SKU profit / inventory health / anomaly diagnosis cockpit for cross-border e-commerce.

把多个店铺、多种货币、表头各不相同的订单/退款/广告/库存/结算表格，
统一成一套口径，算出每个 SKU 的真实利润与断货风险，识别异常，
并让 AI 只负责"解释原因、给建议"，不确定时转人工复核。

## 核心设计

**规则算数，AI 解释，人来拍板。**

- 财务数字由规则引擎计算：可复现、可审计、零成本、不产生幻觉
- AI 只回答"为什么可能异常、接下来做什么"，且必须引用已算好的指标
- 每个结论都带证据（指标 + 窗口 + 数值），可追溯到原始行

## 当前进度

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| P0 | 工程骨架、CI、环境 | ✅ |
| P1 | 数据层：生成器 / 映射 / 校验 / 入库 / 质量报告 | ✅ |
| P2 | 指标层（利润、库存、看板） | ⏳ |
| P3 | 异常识别（10 条规则 + 评估） | ⏳ |
| P4 | AI 归因 + 人审闭环 | ⏳ |

## 已跑出来的数据

一次 `generate + ingest`（seed=20241119）：

| 指标 | 值 |
| --- | --- |
| 规模 | 3 店 × 200 SKU × 90 天 |
| 订单行 | 82,651 |
| 总入库行 | 334,945 |
| 脏数据注入 | 15 行（8 类） |
| 脏数据召回 | **100%**（15/15，含原因码匹配） |
| 被拒行 | 16（多出 1 行是级联：坏订单行的退款成了孤儿） |
| 幂等 | 重跑 15 个文件全部 skip，行数不变 |
| 全量入库耗时 | ~34 秒 |

## 快速开始

```powershell
# 1. 环境（首次）
uv sync

# 2. 生成演示数据（3 店 × 200 SKU × 90 天，约 6 秒）
uv run python -m sku_cockpit generate

# 3. 建表 + 入库 + 质量报告（约 35 秒）
uv run python -m sku_cockpit ingest --drop --report

# 4. 看报告
cat reports/data_quality.md
```

没有 MySQL 也能验证数据层：`uv run pytest` 里除 `integration` 标记外的用例都不依赖数据库。

数据库连接串放在 `.env`（`.env.example` 有模板），默认账号只对 `sku_cockpit` 库有权限。

## 目录

```
src/sku_cockpit/
├─ cli.py            generate / ingest / report 三个入口
├─ config.py         环境配置（pydantic-settings）
├─ schema.py         SQLAlchemy 表定义（维度 / 事实 / 控制 / 暂存）
├─ db.py             engine 与建表
└─ data/
   ├─ constants.py   店铺、类目、促销日历等业务常量
   ├─ generator.py   带 seed 的模拟数据生成器（含异常与脏数据埋点）
   ├─ mapping.py     店铺表头 → 标准字段（YAML 驱动）
   ├─ validation.py  三档校验：结构 / 业务 / 统计
   ├─ ingest.py      raw → 校验 → staging，幂等
   └─ quality.py     质量报告与脏数据召回对照
config/mapping/      每家店铺一份表头映射
tests/               生成器、校验、映射 + MySQL 集成测试
docs/                环境说明、数据层设计
```

## 设计要点

**粒度用订单行。** 订单可多 SKU、可拆单、可部分退款，用订单粒度算不出 SKU 利润。

**金额一律 `DECIMAL(18,4)` + 显式币种。** 不用 float，汇率换算只有一个入口。

**时间存两份**：`*_at_utc` 和 `*_date_local`，报表按 marketplace 本地日切分。

**校验分三档**：结构 / 业务违规 → 拒绝并给原因码；统计离群 → 保留并打标（离群可能正是要找的异常）。

**坏数据不静默丢弃**：每行被拒都进 `reject_row`，带原因码、原始数据和源行号。

## 数据与合规

- 不接触任何真实经营数据；演示数据由带 seed 的生成器产生
- 不抓取需登录页面、不绕过反爬
- 密钥只走 `.env`，仓库里只有 `.env.example`

## 文档

- [环境说明](docs/environment.md)
- [数据层设计](docs/design-p1-data-layer.md)

## License

MIT
