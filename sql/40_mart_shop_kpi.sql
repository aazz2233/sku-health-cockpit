-- ---------------------------------------------------------------------------
-- mart_shop_kpi：店铺 × 月 的经营概览
-- 风险 SKU 数 = 当月出现过断货风险/低库存/缺货的 SKU 数（去重）
-- ---------------------------------------------------------------------------

INSERT INTO mart_shop_kpi (
    shop_id, month,
    gmv_cny, net_revenue_cny, net_profit_cny, net_margin_pct,
    orders, units, aov_cny, tacos_pct, sku_count, risk_sku_count
)
SELECT
    shop_id,
    CAST(DATE_FORMAT(date_local, '%Y-%m-01') AS DATE) AS month,
    SUM(gmv_cny) AS gmv_cny,
    SUM(net_revenue_cny) AS net_revenue_cny,
    SUM(net_profit_cny) AS net_profit_cny,
    CASE WHEN SUM(net_revenue_cny) = 0 THEN NULL
         ELSE SUM(net_profit_cny) / SUM(net_revenue_cny) END AS net_margin_pct,
    SUM(orders) AS orders,
    SUM(units) AS units,
    CASE WHEN SUM(orders) = 0 THEN NULL
         ELSE SUM(gmv_cny) / SUM(orders) END AS aov_cny,
    CASE WHEN SUM(net_revenue_cny) = 0 THEN NULL
         ELSE SUM(ad_spend_cny) / SUM(net_revenue_cny) END AS tacos_pct,
    COUNT(DISTINCT sku_id) AS sku_count,
    COUNT(DISTINCT CASE WHEN stock_status IN ('STOCKOUT_RISK', 'OUT_OF_STOCK', 'LOW')
                        THEN sku_id END) AS risk_sku_count
FROM mart_sku_daily
GROUP BY shop_id, CAST(DATE_FORMAT(date_local, '%Y-%m-01') AS DATE);
