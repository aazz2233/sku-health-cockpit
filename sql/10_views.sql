-- ---------------------------------------------------------------------------
-- 基础视图：把 staging 变成"SKU × 店铺 × 日"的稠密事实
--
-- 为什么要稠密网格：rolling window 只有在每一天都有行时才正确。
-- 没有销售的 SKU-日 如果缺行，7 日均值会跨过空缺日，算出来偏大。
-- ---------------------------------------------------------------------------

-- 1. SKU 单位成本（采购 + 头程按件分摊）
CREATE OR REPLACE VIEW v_sku_cost AS
SELECT
    s.sku_id,
    s.category,
    s.unit_cbm,
    s.unit_cost_cny,
    COALESCE(sh.qty_sum, 0) AS shipped_qty,
    COALESCE(sh.freight_sum, 0) AS freight_cny,
    CASE WHEN COALESCE(sh.qty_sum, 0) > 0
         THEN sh.freight_sum / sh.qty_sum
         ELSE 0 END AS unit_freight_cny,
    s.unit_cost_cny
      + CASE WHEN COALESCE(sh.qty_sum, 0) > 0
             THEN sh.freight_sum / sh.qty_sum
             ELSE 0 END AS unit_total_cost_cny
FROM dim_sku s
LEFT JOIN (
    SELECT sku_id, SUM(qty) AS qty_sum, SUM(freight_cny) AS freight_sum
    FROM dim_shipment
    GROUP BY sku_id
) sh ON sh.sku_id = s.sku_id;

-- 2. 稠密网格：每个在售 SKU × 每个店铺 × 每一天
CREATE OR REPLACE VIEW v_grid AS
SELECT l.sku_id, l.shop_id, d.date_key AS date_local
FROM (SELECT DISTINCT sku_id, shop_id FROM dim_listing) l
CROSS JOIN dim_date d;

-- 3. 订单日聚合
CREATE OR REPLACE VIEW v_orders_daily AS
SELECT
    o.sku_id,
    o.shop_id,
    o.order_date_local AS date_local,
    SUM(o.qty) AS units,
    COUNT(DISTINCT o.order_id) AS orders,
    SUM(o.item_total_local) AS gmv_local,
    SUM(COALESCE(o.item_total_local * fx.rate_to_cny, 0)) AS gmv_cny,
    SUM(CASE WHEN fx.rate_to_cny IS NULL THEN 1 ELSE 0 END) AS fx_missing_rows
FROM staging_order_item o
LEFT JOIN dim_fx fx
    ON fx.currency = o.currency AND fx.date_key = o.order_date_local
GROUP BY o.sku_id, o.shop_id, o.order_date_local;

-- 4. 退款日聚合（按退款发生日冲减，不追溯原订单月）
CREATE OR REPLACE VIEW v_refunds_daily AS
SELECT
    r.sku_id,
    r.shop_id,
    r.refund_date_local AS date_local,
    SUM(COALESCE(r.amount_local * fx.rate_to_cny, 0)) AS refund_cny,
    SUM(CASE WHEN r.restockable = 0 THEN r.qty ELSE 0 END) AS damaged_units
FROM staging_refund r
LEFT JOIN dim_fx fx
    ON fx.currency = r.currency AND fx.date_key = r.refund_date_local
GROUP BY r.sku_id, r.shop_id, r.refund_date_local;

-- 5. 广告日聚合
CREATE OR REPLACE VIEW v_ads_daily AS
SELECT
    a.sku_id,
    a.shop_id,
    a.date_local,
    SUM(COALESCE(a.spend_local * fx.rate_to_cny, 0)) AS ad_spend_cny
FROM staging_ad_spend a
LEFT JOIN dim_fx fx
    ON fx.currency = a.currency AND fx.date_key = a.date_local
GROUP BY a.sku_id, a.shop_id, a.date_local;

-- 6. 平台费用日聚合（取结算单真实扣费，金额为负，这里转正）
CREATE OR REPLACE VIEW v_fees_daily AS
SELECT
    oi.sku_id,
    s.shop_id,
    s.date_local,
    SUM(CASE WHEN s.fee_type = 'referral'
             THEN -s.amount_local * fx.rate_to_cny ELSE 0 END) AS referral_cny,
    SUM(CASE WHEN s.fee_type = 'fba_fulfillment'
             THEN -s.amount_local * fx.rate_to_cny ELSE 0 END) AS fba_cny
FROM staging_settlement s
JOIN staging_order_item oi ON oi.order_item_id = s.order_item_id
LEFT JOIN dim_fx fx
    ON fx.currency = s.currency AND fx.date_key = s.date_local
WHERE s.amount_local < 0
GROUP BY oi.sku_id, s.shop_id, s.date_local;

-- 7. 汇兑影响：收入按订单日汇率、平台按结算日汇率，差额单列
CREATE OR REPLACE VIEW v_fx_effect_daily AS
SELECT
    oi.sku_id,
    oi.shop_id,
    oi.order_date_local AS date_local,
    SUM(oi.item_total_local * (fxs.rate_to_cny - fxo.rate_to_cny)) AS fx_effect_cny
FROM staging_order_item oi
JOIN staging_settlement s
    ON s.order_item_id = oi.order_item_id AND s.fee_type = 'referral'
LEFT JOIN dim_fx fxo
    ON fxo.currency = oi.currency AND fxo.date_key = oi.order_date_local
LEFT JOIN dim_fx fxs
    ON fxs.currency = s.currency AND fxs.date_key = s.date_local
GROUP BY oi.sku_id, oi.shop_id, oi.order_date_local;

-- 8. 库存快照（按 SKU-店-日 合并仓库）
CREATE OR REPLACE VIEW v_stock_daily AS
SELECT sku_id, shop_id, date_local, SUM(available_qty) AS available_qty
FROM staging_inventory_snapshot
GROUP BY sku_id, shop_id, date_local;

-- 9. 稠密事实：网格左连所有聚合
CREATE OR REPLACE VIEW v_daily_fact AS
SELECT
    g.sku_id,
    g.shop_id,
    g.date_local,
    COALESCE(o.units, 0) AS units,
    COALESCE(o.orders, 0) AS orders,
    COALESCE(o.gmv_local, 0) AS gmv_local,
    COALESCE(o.gmv_cny, 0) AS gmv_cny,
    COALESCE(r.refund_cny, 0) AS refund_cny,
    COALESCE(r.damaged_units, 0) AS damaged_units,
    COALESCE(a.ad_spend_cny, 0) AS ad_spend_cny,
    COALESCE(f.referral_cny, 0) AS referral_fee_cny,
    COALESCE(f.fba_cny, 0) AS fba_fee_cny,
    COALESCE(x.fx_effect_cny, 0) AS fx_effect_cny,
    st.available_qty,
    c.unit_total_cost_cny,
    c.unit_cbm,
    c.category,
    fs.storage_rate_per_cbm_month,
    fxr.rate_to_cny AS fx_rate,
    (COALESCE(o.fx_missing_rows, 0) > 0) AS fx_missing
FROM v_grid g
LEFT JOIN v_orders_daily o
    ON o.sku_id = g.sku_id AND o.shop_id = g.shop_id AND o.date_local = g.date_local
LEFT JOIN v_refunds_daily r
    ON r.sku_id = g.sku_id AND r.shop_id = g.shop_id AND r.date_local = g.date_local
LEFT JOIN v_ads_daily a
    ON a.sku_id = g.sku_id AND a.shop_id = g.shop_id AND a.date_local = g.date_local
LEFT JOIN v_fees_daily f
    ON f.sku_id = g.sku_id AND f.shop_id = g.shop_id AND f.date_local = g.date_local
LEFT JOIN v_fx_effect_daily x
    ON x.sku_id = g.sku_id AND x.shop_id = g.shop_id AND x.date_local = g.date_local
LEFT JOIN v_stock_daily st
    ON st.sku_id = g.sku_id AND st.shop_id = g.shop_id AND st.date_local = g.date_local
LEFT JOIN v_sku_cost c ON c.sku_id = g.sku_id
JOIN dim_shop sh ON sh.shop_id = g.shop_id
LEFT JOIN dim_fee_schedule fs
    ON fs.category = c.category
   AND fs.marketplace = sh.marketplace
   AND fs.effective_from <= g.date_local
   AND (fs.effective_to IS NULL OR fs.effective_to >= g.date_local)
LEFT JOIN dim_fx fxr
    ON fxr.currency = sh.currency AND fxr.date_key = g.date_local;
