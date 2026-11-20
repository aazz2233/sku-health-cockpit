-- ---------------------------------------------------------------------------
-- mart_sku_monthly：先聚合金额，再算比率。
-- 注意：绝不能用 AVG(日利润率)，那算的是"比率的平均"，数学上错误。
-- ---------------------------------------------------------------------------

INSERT INTO mart_sku_monthly (
    sku_id, shop_id, month,
    units, orders, gmv_cny, refund_cny, net_revenue_cny,
    cogs_cny, fee_total_cny, ad_spend_cny,
    gross_profit_cny, net_profit_cny,
    gross_margin_pct, net_margin_pct, tacos_pct
)
SELECT
    sku_id,
    shop_id,
    CAST(DATE_FORMAT(date_local, '%Y-%m-01') AS DATE) AS month,
    SUM(units) AS units,
    SUM(orders) AS orders,
    SUM(gmv_cny) AS gmv_cny,
    SUM(refund_cny) AS refund_cny,
    SUM(net_revenue_cny) AS net_revenue_cny,
    SUM(cogs_cny) AS cogs_cny,
    SUM(referral_fee_cny + fba_fee_cny + storage_fee_cny) AS fee_total_cny,
    SUM(ad_spend_cny) AS ad_spend_cny,
    SUM(gross_profit_cny) AS gross_profit_cny,
    SUM(net_profit_cny) AS net_profit_cny,
    CASE WHEN SUM(net_revenue_cny) = 0 THEN NULL
         ELSE SUM(gross_profit_cny) / SUM(net_revenue_cny) END AS gross_margin_pct,
    CASE WHEN SUM(net_revenue_cny) = 0 THEN NULL
         ELSE SUM(net_profit_cny) / SUM(net_revenue_cny) END AS net_margin_pct,
    CASE WHEN SUM(net_revenue_cny) = 0 THEN NULL
         ELSE SUM(ad_spend_cny) / SUM(net_revenue_cny) END AS tacos_pct
FROM mart_sku_daily
GROUP BY sku_id, shop_id, CAST(DATE_FORMAT(date_local, '%Y-%m-01') AS DATE);
