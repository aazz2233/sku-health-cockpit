# 指标口径（P2 规格说明）

> 状态：待确认。确认后按本文实现 `sql/` 与 `mart_*` 表。
> 数据来源：`staging_*` 与 `dim_*`（P1 已就绪，82,651 订单行 / 3 店 / 90 天）

---

## 一、三条铁律（P1 已定，这里只是重申）

1. **粒度**：`mart_sku_daily` 一行 = 一个 SKU × 一个店铺 × 一个业务日
2. **金额**：一律 `DECIMAL(18,4)`，人民币口径字段以 `_cny` 结尾，本币口径以 `_local` 结尾，不混用
3. **时间**：业务日取 `*_date_local`（marketplace 当地日），不取 UTC

---

## 二、币种与汇率

| 规则 | 定义 |
| --- | --- |
| 汇率来源 | `dim_fx`（本币 → CNY，按日） |
| 收入折算日 | **订单日** `order_date_local`（经营口径：确认收入当天） |
| 费用折算日 | **结算日** `settlement.date_local` |
| 退款折算日 | **退款发生日** `refund_date_local`（冲减发生当期，不追溯原订单） |
| 汇率缺失 | 取该币种最近一个可用交易日；再缺则整行标记 `fx_missing` 并排除出汇总 |

**汇兑影响 `fx_effect_cny`**：同一笔交易，收入按订单日汇率、平台费用按结算日汇率折算，两者差额单独成列。

> 现实里 `fx_effect` 来自"确认收入"与"实际结汇"的时间差。我们的模拟数据里两个日期相同，所以该列接近 0——但**保留这一列**，接真实结算单时才有地方放。这是"口径完整"与"数据完整"的区分。

---

## 三、收入侧

| 指标 | 公式 | 说明 |
| --- | --- | --- |
| `gmv_local` | `Σ(item_total_local)` | 订单行金额合计，含税/含运费（本数据无税） |
| `gmv_cny` | `Σ(item_total_local × fx(order_date))` | |
| `refund_amount_local` | `Σ(refund.amount_local)` | 按退款发生日归集 |
| `refund_amount_cny` | `Σ(refund.amount_local × fx(refund_date))` | |
| `net_revenue_cny` | `gmv_cny − refund_amount_cny` | **净收入**，后续利润的起点 |

**边界规则**
- 退款跨月：只冲减退款发生月，不追溯原订单月（经营口径）
- 退款单找不到订单行：P1 已在入库时拒绝，不会进入本层
- 部分退款：`refund.qty < order_item.qty` 时按实际退款金额冲减，不改销量

---

## 四、成本侧

### 4.1 商品成本 `cogs_cny`

单位成本由两部分构成，按 **SKU 维度移动加权平均**：

```
unit_cost_cny   = dim_sku.unit_cost_cny                    # 采购成本（已含在商品主数据里）
unit_freight    = Σ(dim_shipment.freight_cny) / Σ(dim_shipment.qty)   # 头程按件分摊
unit_total_cost = unit_cost_cny + unit_freight

cogs_cny = Σ(qty × unit_total_cost)
```

**为什么用移动加权而不是批次核销**：我们没有逐批出库记录，FIFO 需要批次消耗明细，属于"数据不存在却硬算"。加权平均是本数据支持下最诚实的做法，且偏差可控。README 里会写明这一点。

**头程口径**：`freight_cny` 是整批运费，除以批次件数得到单件头程；同一 SKU 多批次时按总量加权（不是简单平均）。

### 4.2 平台费用（优先用真实结算，缺失才用费率表估算）

| 费用 | 取值顺序 | 公式 |
| --- | --- | --- |
| 佣金 `referral_fee_cny` | ① `staging_settlement(fee_type='referral')` ② `dim_fee_schedule.referral_rate × item_total`，下限 `referral_min` | 取绝对值 |
| FBA 配送费 `fba_fee_cny` | ① `staging_settlement(fee_type='fba_fulfillment')` ② `dim_fee_schedule.fba_fee_local` | 取绝对值 |
| 仓储费 `storage_fee_cny` | 按库存快照估算（见下） | |

**佣金规则**：`max(item_total × referral_rate, referral_min)`。费率表按 `(类目, marketplace, 生效区间)` 匹配，**按订单日取当时生效的版本**（SCD2）。

**仓储费估算**：
```
daily_storage_cny = available_qty
                  × unit_cbm
                  × storage_rate_per_cbm_month
                  / 30
                  × fx(当日)
```
`unit_cbm` 目前不在 `dim_sku` 里，需要新增字段（体积按类目重量估算包材后落库，见"七、待补数据"）。这是**估算而非账单**，README 与看板都会标注。

### 4.3 广告费 `ad_spend_cny`

```
ad_spend_cny = Σ(staging_ad_spend.spend_local × fx(date_local))
```

广告按 SKU 归集（我们的数据 SKU 维度齐全）。真实场景里 campaign 可能跨 SKU，届时按 `attributed_sales` 比例分摊——本数据不需要，但要在代码里留注释说明。

### 4.4 退货货损 `return_loss_cny`

```
若 refund.restockable = False:  return_loss_cny = refund.qty × unit_total_cost
若 refund.restockable = True:   return_loss_cny = 0
```

**为什么不用"退货处理费"这个科目**：那是平台真实扣费，本数据里没有账单依据，编一个比例出来就是假数。而 `restockable` 字段是真实存在的，用它算货损有据可依。

---

## 五、利润公式（完整版）

```
净收入        net_revenue_cny    = gmv_cny − refund_cny
商品成本      cogs_cny           = Σ qty × (采购 + 头程/件)
平台佣金      referral_fee_cny
FBA 配送      fba_fee_cny
仓储费        storage_fee_cny
广告费        ad_spend_cny
退货货损      return_loss_cny
─────────────────────────────────────────────
毛利          gross_profit_cny   = net_revenue_cny − cogs_cny − return_loss_cny
净利          net_profit_cny    = gross_profit_cny
                                 − referral_fee_cny − fba_fee_cny
                                 − storage_fee_cny − ad_spend_cny
                                 + fx_effect_cny
毛利率        gross_margin_pct   = gross_profit_cny / net_revenue_cny
净利率        net_margin_pct    = net_profit_cny / net_revenue_cny
TACOS         tacos_pct          = ad_spend_cny / net_revenue_cny
```

**对账恒等式**（写进测试）：

```
net_profit = net_revenue − cogs − referral − fba − storage − ads − return_loss + fx_effect
```

任何一行不满足这条恒等式，说明 SQL 有 bug，而不是"口径不同"。

**分母为 0 的处理**：`net_revenue = 0` 时，利润率返回 `NULL` 而不是 0 或 inf。0 会污染平均值，inf 会炸看板。

---

## 六、库存指标

| 指标 | 公式 | 规则 |
| --- | --- | --- |
| `daily_units_7d` | 近 7 天销量均值 | 不足 7 天按实际天数 |
| `daily_units_30d` | 近 30 天销量均值 | 同上 |
| `daily_units_blend` | `0.5 × 7d + 0.5 × 30d` | 抗突发，兼顾趋势 |
| `days_of_cover` | `available_qty / daily_units_blend` | 分母为 0 → `NULL` |
| `lead_time_days` | 类目常量（30 / 45 天） | 无真实交期数据，放 `config/inventory.yaml` |
| `safety_stock` | `1.65 × σ(近30天日销量) × √lead_time` | 95% 服务水平 |
| `reorder_point` | `daily_units_blend × lead_time + safety_stock` | |
| `stock_status` | 见下 | 枚举，不是分数 |

**`stock_status` 判定顺序**（先命中先返回）：

1. `available_qty <= 0` → `OUT_OF_STOCK`
2. `days_of_cover < lead_time_days` → `STOCKOUT_RISK`
3. `days_of_cover < lead_time_days × 1.5` → `LOW`
4. `days_of_cover > 180` → `OVERSTOCK`
5. 其余 → `HEALTHY`

**为什么用枚举而不是连续分数**：运营的动作是离散的（补货 / 清库存 / 不动），分数还要再拍一次阈值才可用，多此一举。

---

## 七、表结构

### 7.1 `mart_sku_daily`

| 字段 | 类型 | 来源 |
| --- | --- | --- |
| `sku_id` / `shop_id` / `date_local` | 联合主键 | |
| `units` / `orders` | INT | 订单行 |
| `gmv_local` `gmv_cny` `refund_cny` `net_revenue_cny` | DECIMAL(18,4) | 收入侧 |
| `cogs_cny` `referral_fee_cny` `fba_fee_cny` `storage_fee_cny` `ad_spend_cny` `return_loss_cny` `fx_effect_cny` | DECIMAL(18,4) | 成本侧 |
| `gross_profit_cny` `net_profit_cny` | DECIMAL(18,4) | 派生 |
| `gross_margin_pct` `net_margin_pct` `tacos_pct` | DECIMAL(9,6) | 派生，可为 NULL |
| `available_qty` `daily_units_blend` `days_of_cover` `reorder_point` | 库存 | 快照 + 窗口 |
| `stock_status` | VARCHAR(16) | 枚举 |
| `fx_missing` | BOOLEAN | 数据质量标记 |

### 7.2 `mart_sku_monthly`
按 `sku_id × shop_id × 月` 聚合日表；注意 `days_of_cover` 取**月末快照**而不是日平均（库存是时点量，不能平均）。`margin_pct` 由月汇总的金额重算，**不是**日比率的平均。

> 这条规则很关键：先聚合金额、再算比率。反过来会得到"平均的比率"，数学上是错的。

### 7.3 `mart_shop_kpi`
按 `shop_id × 月`：GMV、净收入、净利、订单数、客单价、TACOS、异常 SKU 数。

---

## 八、SQL 分层与执行顺序

```
sql/
├─ mart/
│  ├─ 10_sku_daily_base.sql      订单行 + 退款 + 广告 + 结算 → 基础聚合
│  ├─ 20_sku_daily_cost.sql      接入单位成本（采购+头程）与仓储
│  ├─ 30_mart_sku_daily.sql      利润与库存指标落表
│  ├─ 40_mart_sku_monthly.sql    月汇总
│  └─ 50_mart_shop_kpi.sql       店铺 KPI
└─ run.py                        Jinja 渲染 + 按序执行 + 幂等（先删后插）
```

规则：**每张 mart 表都是全量重算**（数据量 8 万行，几秒钟）。增量留给以后，现在做增量只会引入 bug。

---

## 九、对账方案（三层，缺一不可）

| 层 | 方法 | 通过标准 |
| --- | --- | --- |
| L1 恒等式 | 逐行校验 `net_profit = 各项相加减` | 100% 行通过（用 SQL 一条语句筛出违反行，必须为 0） |
| L2 反向复算 | 用生成器写入的真值（成本、费率、广告额、退款额）重算期望利润，与 mart 比对 | 偏差 < 0.01 元，且给出比对样本 |
| L3 交叉校验 | 用 `staging_settlement`（平台实际扣费）核对 mart 里的佣金/FBA | 差异为 0（因为本数据结算即真值） |

L2 需要生成器暴露"真值"：`skus.csv` 里有 `unit_cost_cny`，`shipments.csv` 里有运费与件数，`fee_schedule.csv` 有费率。所以复算脚本可以独立于 mart 实现——**用两套代码算同一件事，才有对账意义**。

---

## 十、验收标准（P2 DoD）

| # | 项 | 判据 |
| --- | --- | --- |
| 1 | 恒等式 | L1 违反行数 = 0 |
| 2 | 反向复算 | 抽样 50 行，最大偏差 < 0.01 元 |
| 3 | 交叉校验 | 佣金/FBA 与结算表差异 = 0 |
| 4 | 合理性 | 毛利率 35%~50%、**净利率 5%~12%（全店约 9%）** |
| 5 | 空值规则 | `net_revenue=0` 的行，利润率字段为 NULL 而非 0 |
| 6 | 幂等 | 连跑两次，行数与金额不变 |
| 7 | 性能 | 全量重算 < 10 秒 |

第 4 条不是形式主义：如果算出来净利率 40% 或 −20%，几乎一定漏了某项费用。

---

## 十一、待补数据（实现时要改的地方）

1. **`dim_sku.unit_cbm`**：仓储费需要单件体积，现在没有。方案：生成器按 `类目重量 / 200` 估算体积并写入 `dim_sku`，重新生成 + 入库。改动小，但会让仓储费从"拍脑袋"变成"有依据的估算"。
2. **`lead_time_days`**：新增 `config/inventory.yaml`，按类目配置交期与服务水平。
3. 汇率：若某币种某日缺失，用最近可用日（前向填充），并在 `fx_missing` 标记。

---

## 十二、已知简化（写进 README，不藏着）

| 简化 | 影响 | 真实做法 |
| --- | --- | --- |
| 头程用移动加权，不是批次核销 | 单 SKU 成本可能有小幅偏差 | 需要逐批出库记录 |
| 仓储费按快照估算 | 与真实账单有差异 | 接入平台月仓储账单 |
| 收入只做订单日口径 | 无结算口径对比 | 需要 settlement 与 order 的关联键 |
| 退款不追溯原月份 | 月度利润会有轻微错位 | 会计口径需追溯调整 |
| 无增值税/关税 | 不完全等于真实 P&L | 需要税务数据 |

这些不是"没做完"，是**数据边界决定的合理简化**。面试时被问"为什么不用 FIFO"，答案就是"没有出库批次记录，硬算等于编数据"。
