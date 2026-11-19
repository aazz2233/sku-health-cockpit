# P1 数据层设计（数据模型 / 生成器 / 校验）

> 状态：设计稿，待评审后实现
> 范围：只覆盖 P1（数据层）。指标口径细节在 P2 的 `docs/metrics.md`，异常规则在 P3 的 `docs/rules.md`

---

## 一、先定三条"宪法级"约定

数据层出问题，90% 是这三件事没说清楚。

### 1. 粒度：以"订单行"为最小事实单位

| 事实表 | 粒度 | 主键 |
| --- | --- | --- |
| `fact_order_item` | 一行 = 一个订单里的一个 SKU | `order_item_id` |
| `fact_refund` | 一行 = 一次退款/退货事件 | `refund_id` |
| `fact_ad_spend` | 一行 = 一个 campaign 的一天 | `(shop_id, campaign_id, date)` |
| `fact_inventory_snapshot` | 一行 = 一个 SKU 在一个仓的一天 | `(shop_id, sku_id, warehouse, date)` |
| `fact_settlement` | 一行 = 平台的一笔费用明细 | `settlement_id` |

**为什么不用"订单"做事实表**：订单可以多 SKU、可拆单、可部分退款，用订单粒度算不出 SKU 利润，也做不了异常识别。

### 2. 金额：一律 `DECIMAL(18,4)` + 明确币种

- 数据库里绝不用 `float`/`double`。pandas 侧用 `Decimal` 或"整数分"处理，入库前转换。
- 每个金额字段必须搭配一个币种字段，或字段名自带后缀：`amount_local`（当地币）/ `amount_cny`（人民币）。
- 汇率换算只有一个入口函数，不允许在业务代码里随手乘。

### 3. 时间：存两个

- `*_at_utc`：事件发生的 UTC 时间戳，用于排序与跨时区对齐
- `*_date_local`：业务日期（按 marketplace 当地时间），**所有日报/周报/月报都按它分组**

**为什么**：Amazon 的报表按 marketplace 本地日切分。只存 UTC 会出现"美国 6 月 30 日晚上的订单被算进 7 月"这种对不上账的问题。

### 4.（附带）口径分叉要显式标注

经营分析用**下单口径**（`order_time`），财务对账用**结算口径**（`settlement.date`）。两张 marts 都提供，字段名区分：`gmv_order_date` / `gmv_settle_date`。混用是行业里最常见的对账事故。

---

## 二、实体模型

### 2.1 维度表

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| `dim_shop` | `shop_id` `shop_name` `marketplace` `currency` `timezone` `status` | 3 个店铺：US/USD、DE/EUR、UK/GBP |
| `dim_sku` | `sku_id` `parent_asin` `asin` `title` `brand` `category` `product_type` `launch_date` `lifecycle` | 我方商品主数据 |
| `dim_listing` | `listing_id` `sku_id` `shop_id` `asin` `title` `price_local` `status` | **SKU × 店铺**的销售单元；一个 SKU 可在一店多 listing（重复铺货） |
| `dim_fee_schedule` | `category` `marketplace` `referral_rate` `referral_min` `fba_size_tier` `fba_weight_tier` `fba_fee` `storage_rate` `effective_from` `effective_to` | **SCD2 版本化**：费率变了要能按历史重算 |
| `dim_date` | `date` `year` `month` `week` `weekday` `is_promo` | 促销日历放这里 |
| `dim_shipment` | `shipment_id` `sku_id` `qty` `freight_cny` `arrived_at` `warehouse` | 头程批次，用于按件分摊运费 |

### 2.2 事实表

| 表 | 关键字段 |
| --- | --- |
| `fact_order_item` | `order_item_id` `order_id` `shop_id` `sku_id` `listing_id` `qty` `unit_price_local` `item_promo_local` `item_total_local` `currency` `order_at_utc` `order_date_local` `fulfillment` `status` |
| `fact_refund` | `refund_id` `order_item_id` `qty` `amount_local` `currency` `reason_code` `refund_at_utc` `refund_date_local` `restockable` |
| `fact_ad_spend` | `ad_spend_id` `shop_id` `sku_id`(nullable) `campaign_id` `campaign_type` `date_local` `impressions` `clicks` `spend_local` `attributed_sales_local` |
| `fact_inventory_snapshot` | `snapshot_id` `shop_id` `sku_id` `warehouse` `date_local` `available_qty` `inbound_qty` `reserved_qty` |
| `fact_settlement` | `settlement_id` `shop_id` `order_item_id`(nullable) `date_local` `fee_type` `amount_local` `currency` |

`fee_type` 枚举：`referral` `fba_fulfillment` `storage` `ads` `refund_admin` `other`。

### 2.3 管道与控制表

| 表 | 用途 |
| --- | --- |
| `ingest_run` | 每次导入一行：`run_id` `source_file` `file_hash` `dataset` `rows_total` `rows_loaded` `rows_rejected` `started_at` `finished_at` `status` |
| `reject_row` | 被拒行全量留档：`run_id` `row_number` `reason_code` `detail` `raw_row_json` |
| `anomaly_label` | P3 的标注集：`sku_id` `shop_id` `date` `is_anomaly` `anomaly_type` `source`(injected/manual) |

### 2.4 关系

```
dim_sku 1 ── n dim_listing ── n fact_order_item
dim_shop 1 ── n fact_order_item / fact_ad_spend / fact_inventory_snapshot
fact_order_item 1 ── n fact_refund
fact_order_item 1 ── n fact_settlement（结算明细可挂在订单行上）
dim_sku 1 ── n dim_shipment（头程分摊）
```

### 2.5 分层落库

```
raw.*        原始落地：全字段 TEXT，不改一个字节，带 source_file + row_number
   ↓ 字段映射 + 类型转换
staging.*    已解析、已校验，类型正确，可重复加载
   ↓ 业务规则 + 聚合
mart.*       星型模型 + 业务宽表
```

**关键设计**：`raw` 层永不被覆盖或删除。任何"这个数为什么不对"的问题，都能从 mart 一路追到 raw 的某一行。

---

## 三、模拟数据生成器设计

### 3.1 目标与约束

- 规模：3 店 × 200 SKU × 90 天 ≈ **6~10 万订单行**
- 可复现：固定 `seed`，两次生成的文件哈希一致
- 真实性：长尾销量、周内周期、大促脉冲、断货归零、退款滞后
- 可控性：**已知答案**——生成时记录埋进去的异常和脏数据，供 P3 做 ground truth

### 3.2 商品与价格

| 维度 | 设计 |
| --- | --- |
| 类目 | 6 个（家居 / 3C配件 / 户外 / 母婴 / 服饰 / 宠物），退款率与毛利率各不同 |
| 变体 | 30 个父体，每个 2~5 个子 SKU，共享价格带（±15%） |
| 价格 | 类目基准 × 对数正态扰动，保留 2 位小数 |
| 成本 | 采购成本（CNY）+ 头程（按批次分摊），保证大部分 SKU 有 20%~45% 毛利 |
| 生命周期 | 85% active、10% 新品（前 20 天销量爬坡）、5% phaseout（销量线性衰减） |

### 3.3 需求生成

```
日销量 ~ Poisson(λ_sku × weekday_factor × promo_factor × lifecycle_factor × stockout_mask)
```

- `λ_sku`：对数正态（长尾，头部 10% 贡献约 50% 销量）
- `weekday_factor`：周末 1.2~1.4
- `promo_factor`：两个促销窗口（各 7 天），1.8~3.0 倍，前后有"预热/回落"斜坡
- `stockout_mask`：约 5% 的 SKU 在中段有 3~10 天零销量（模拟断货）

### 3.4 退款、广告、库存、结算

| 数据 | 生成规则 |
| --- | --- |
| 退款 | 类目退款率（服饰 12%、3C 5%、家居 4%…）；退款发生在下单后 3~20 天；原因按类目分布（尺寸不符/质量/物流慢/买家后悔/未收到） |
| 广告 | 按 SKU 维度，TACOS 从 N(12%, 5%) 抽样，`spend = sales × tacos`；**另设 3 个 SKU 的 TACOS 冲到 35%~45%**（喂给 R3 规则） |
| 库存 | 期初 + 头程到货 − 销量 − 预留；约 3% 的 SKU-日出现负库存（脏数据，见 3.6） |
| 结算 | 用 `dim_fee_schedule` 生成平台真实扣费，与"按费率表估算"互为校验（对账测试用） |
| 汇率 | USD/CNY 7.05~7.25、EUR/CNY 7.55~7.85、GBP/CNY 8.95~9.35，随机游走 + 轻微漂移 |

### 3.5 埋入的异常（P3 的 ground truth）

| 编号 | 异常类型 | 埋入方式 | 数量 |
| --- | --- | --- | --- |
| A1 | 持续负毛利 | 3 个 SKU 中期成本上调 / 售价下调，净利率持续 −3% 左右 | 5 个 SKU×窗口 |
| A2 | 毛利率骤降 | 某周广告费翻 3 倍 | 3 |
| A3 | 销量断崖 | 某天起销量跌 70% 且不恢复 | 4 |
| A4 | 退款率异常 | 某 SKU 退款率提到 30% | 3 |
| A5 | 断货风险 | 可售天数压到 < 交期 | 6 |
| A6 | 超储滞销 | 一次大额补货后销量归零 | 4 |
| A7 | 重复铺货 | 造 3 组标题高度相似、品牌型号相同的 listing | 3 对 |
| A8 | 变体价差异常 | 同一父体下某个子 SKU 价格高 40% | 2 |

全部写入 `anomaly_label`，字段 `source='injected'`。P3 用它对规则做 recall 评估。

### 3.6 埋入的脏数据（校验层的测试素材）

| 编号 | 类型 | 具体形式 |
| --- | --- | --- |
| D1 | 重复行 | 同一 `order_item_id` 出现 2~3 次 |
| D2 | 缺价格 | `unit_price` 为空 |
| D3 | 未知 SKU | `sku_id` 不在 `dim_sku` 里 |
| D4 | 币种错配 | UK 店铺的订单标成 USD |
| D5 | 负数量 | `qty = -1` |
| D6 | 日期非法 | `2024-02-30`、`13/45/2024` 这类 |
| D7 | 表头脏 | 列名带多余空格 / 换行 / 全角括号 |
| D8 | SKU 大小写不一致 | `SKU-0123` vs `sku-0123` |

**每个文件配一份 `manifest.json`**：记录"第几行是哪种坏数据"。这样校验测试是**断言召回率**，而不是"跑通就行"。

---

## 四、字段映射设计

### 4.1 为什么需要映射层

真实场景里，三个店铺导出三套表头：`Order ID` / `订单编号` / `order_id`。把解析逻辑写死在代码里，每来一个新店铺就要改代码。

### 4.2 配置即数据

`config/mapping/<shop_id>_<dataset>.yaml`：

```yaml
dataset: orders
header_row: 3                    # 有的店铺文件前面有 2 行说明
columns:
  order_id:    ["Order ID", "订单编号", "order id"]
  order_time:  ["Purchase Date", "下单时间"]
  sku_id:      ["SKU", "Seller SKU", "商家SKU"]
  qty:         ["Quantity", "数量"]
  unit_price:  ["Item Price", "单价"]
value_maps:
  currency: {"US$": "USD", "美元": "USD", "€": "EUR"}
date_formats: ["%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M", "%Y/%m/%d"]
```

规则：

- 一个标准字段可以配多个别名，**按顺序匹配，命中即用**
- 匹配不到任何别名 → **整文件失败**，错误信息列出"文件里有、但没被映射的列"
- 同一别名映射到两个标准字段 → 启动即报错（防手误）
- 映射配置有单测：用每份样例文件跑一遍，断言 100% 列被覆盖

---

## 五、校验设计

### 5.1 三层校验，处理方式不同

| 层 | 检查什么 | 失败处理 | 理由 |
| --- | --- | --- | --- |
| 结构 | 类型、非空、范围、枚举、主键唯一 | **拒绝该行** + 原因码 | 数据本身不可用 |
| 业务 | 跨表引用、金额勾稽、时间先后、币种一致 | **拒绝该行** + 原因码 | 违反业务不变量，算下去必错 |
| 统计 | z-score 离群、毛利率越界 | **保留并打 flag** | 离群可能是真实异常（正是我们要找的），不能丢 |

### 5.2 业务规则清单

| 编号 | 规则 | 违反时 |
| --- | --- | --- |
| B1 | `order.total = Σ(item_total)`，误差 ≤ 0.01 | 拒绝订单内所有行 |
| B2 | `order_item.sku_id` 必须存在于 `dim_sku` | 拒绝该行 |
| B3 | `refund.order_item_id` 存在，且 `Σrefund.qty ≤ item.qty` | 拒绝该退款 |
| B4 | `order_time ≤ ship_time ≤ refund_time` | 拒绝该行 |
| B5 | `0 < unit_price ≤ 类目 p99 × 3` | 拒绝该行 |
| B6 | 订单币种 = 店铺币种 | 拒绝该行 |
| B7 | `settlement.amount_local ≤ 0`（费用为负） | 拒绝该行 |

### 5.3 原因码

`reject_row.reason_code` 用固定枚举，便于统计：

```
STRUCT_NULL / STRUCT_TYPE / STRUCT_RANGE / STRUCT_ENUM / STRUCT_DUP_KEY
BIZ_BALANCE / BIZ_FK_MISSING / BIZ_TIME_ORDER / BIZ_CURRENCY / BIZ_QTY_EXCEED / BIZ_PRICE_RANGE
```

每行还带 `detail`（如 `qty=-3 < 0`）和 `raw_row_json`。

### 5.4 拒绝策略

- **不静默丢数据**：每行被拒都写 `reject_row`
- **不整批回滚**（除非是"整文件级"错误：表头映射失败、文件损坏）
- 一次 ingest 结束输出：`rows_total` / `rows_loaded` / `rows_rejected` / 按 `reason_code` 的分布

---

## 六、幂等与可重跑

| 层级 | 机制 |
| --- | --- |
| 文件级 | `file_hash = sha256(文件)`；`ingest_run` 里已存在同 hash 且 `status=success` → 跳过，日志写 `skipped` |
| 行级 | 按自然主键 upsert（`order_item_id` 等），重跑不产生重复行 |
| 结果级 | mart 表全量重算（P1 数据量下没问题）；P2 若变慢再改增量 |

**验证方式**：同一份文件跑两遍 → 第二遍 `rows_loaded = 0`，mart 表行数不变。

---

## 七、验收标准（P1 的 DoD）

| # | 验收项 | 判据 |
| --- | --- | --- |
| 1 | 生成可复现 | 同 seed 跑两次，输出文件 sha256 一致 |
| 2 | 规模达标 | 订单行 6~10 万，90 天 × 3 店 × 200 SKU |
| 3 | 脏数据可归因 | 8 类坏数据全部被拦，`reject_row` 原因码与 manifest 完全一致 |
| 4 | 业务规则有效 | B1~B7 每条都有单测，且都能构造出触发案例 |
| 5 | 幂等 | 重跑同一文件，`rows_loaded=0`，mart 行数不变 |
| 6 | 可追溯 | 任取 mart 一行，能追到 raw 的 `source_file + row_number` |
| 7 | 埋点可查 | `anomaly_label` 里 A1~A8 的数量与生成器设定一致 |
| 8 | 无 float 金额 | 全链路金额字段为 `DECIMAL`/`Decimal`，单测断言 |

---

## 八、需要你拍板的三个点

| # | 问题 | 我的建议 | 备选 |
| --- | --- | --- | --- |
| 1 | 数据量 | 200 SKU × 90 天（约 8 万行） | 减到 100 SKU 跑得快，但长尾不明显 |
| 2 | 促销日历 | 只做 2 个促销窗口（Prime Day 类） | 加黑五/圣诞，更真实但更复杂 |
| 3 | 汇率来源 | 生成器内置随机游走（离线可跑） | 用真实历史汇率（更可信，但要联网抓一次并存成 CSV） |

确认后我按这个设计实现 P1：先 `schema` + 生成器，再映射与校验，最后入库与质量报告。
