# 看板与日报（P5）

> 看板：`src/sku_cockpit/app/dashboard.py`（Streamlit · 4 页）
> 日报：`src/sku_cockpit/report.py`（Markdown + Excel）
> 一键启动：`scripts/up.ps1`

---

## 一、四个页面在回答什么问题

| 页面 | 回答的问题 | 数据来源 |
| --- | --- | --- |
| 总览 | 今天生意怎么样？哪里在报警？ | `mart_sku_daily` 按日汇总 + `anomaly_event` |
| SKU 健康 | 哪些 SKU 在亏钱/断货/积压？ | 最新一天的快照 + 近 30 天汇总 |
| SKU 下钻 | 这个 SKU 为什么变成这样？ | 单 SKU 日序列 + 成本结构 + 该 SKU 的诊断 |
| 人审队列 | AI 说的这些原因，认不认？ | `diagnosis`（未处理的）+ 写回 `review_feedback` |

设计上刻意不做"大而全的 BI"：**每页只回答一个问题**，运营点开就知道该做什么。

---

## 二、数据一致性

看板和日报读的是**同一套 mart 表**，不做二次计算。所以：

- 看板上看到的净利率，和日报里写的，永远一致
- 指标口径有任何变化，只需要改 `sql/`，两边同时生效

这是"单一事实来源"的实际收益——不是架构洁癖，是避免"报表打架"。

---

## 三、人审闭环怎么落地

1. 队列只显示 `need_human_review = true` 且**没有反馈记录**的诊断
2. 点「通过 / 驳回」写入 `review_feedback`（`decision` + `note` + `reviewer` + 时间）
3. 写入后清缓存并刷新，卡片从队列消失

这张表是后续优化的依据：
- 哪些规则的诊断总被驳回 → 规则阈值有问题
- 哪类建议从没被采纳 → 提示词或指标包缺东西

**没有反馈闭环的 AI 功能，上线三个月后一定退化成噪声源。**

---

## 四、日报内容

`uv run python -m sku_cockpit report-daily`

输出 `reports/daily_<日期>.md` 与 `.xlsx`：

| 部分 | 内容 |
| --- | --- |
| 核心指标 | GMV / 净收入 / 净利 / 净利率 / 订单数 / TACOS，对比上周同日 |
| 今日异常 | 当天 P0/P1，按 7 日利润影响排序 |
| 待人工复核 | 未处理诊断的前 5 条 |
| 亏损 SKU | 近 7 天净利为负的 Top 10（净收入 ≤ 0 时利润率显示 `—`） |
| 库存风险 | 断货风险 / 缺货 SKU 数 |
| Excel | KPI / 今日异常 / 待审诊断 / 亏损SKU 四个 sheet |

---

## 五、截图怎么来的

`scripts/screenshots.py` 用 Playwright 驱动**本机已安装的 Edge**（不需要下载浏览器内核）：

```powershell
uv pip install playwright
uv run streamlit run src/sku_cockpit/app/dashboard.py
$env:TMP='D:\...\.cache\tmp'; $env:TEMP=$env:TMP   # 沙箱里系统临时目录不可写
uv run python scripts/screenshots.py
```

输出到 `docs/images/`，README 直接引用。

---

## 六、已知取舍

| 取舍 | 说明 |
| --- | --- |
| 每次访问全量拉 54,000 行到内存 | 演示规模下 1~2 秒，缓存 120 秒；生产应改成按需查询或预聚合 |
| 看板不做权限控制 | 内部工具的最小版本，多租户/角色不在本次范围 |
| 人审没有批量操作 | 一次一条，20 条/页；量大时应该加批量通过 |
| 日报没有定时任务 | 设计上由调度器触发（cron / 计划任务），不在应用内做 |
