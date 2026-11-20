-- ---------------------------------------------------------------------------
-- mart_sku_daily：SKU × 店铺 × 日 的利润与库存健康
--
-- 口径见 docs/metrics.md。核心恒等式：
--   net_profit = net_revenue
--              - cogs - referral - fba - storage - ads - return_loss
--              + fx_effect
-- 任何一行不满足，说明 SQL 有 bug（对账测试会逐行校验）。
--
-- 模板变量：
--   lead_time_union : (category, lead_days) 的派生表，来自 config/inventory.yaml
--   z_score         : 服务水平对应的 z 值
-- ---------------------------------------------------------------------------

INSERT INTO mart_sku_daily (
    sku_id, shop_id, date_local,
    units, orders, gmv_local, gmv_cny, refund_cny, net_revenue_cny,
    cogs_cny, referral_fee_cny, fba_fee_cny, storage_fee_cny,
    ad_spend_cny, return_loss_cny, fx_effect_cny,
    gross_profit_cny, net_profit_cny,
    gross_margin_pct, net_margin_pct, tacos_pct,
    available_qty, daily_units_blend, days_of_cover, reorder_point,
    stock_status, fx_missing
)
SELECT
    b.sku_id,
    b.shop_id,
    b.date_local,
    b.units,
    b.orders,
    b.gmv_local,
    b.gmv_cny,
    b.refund_cny,
    b.net_revenue_cny,
    b.cogs_cny,
    b.referral_fee_cny,
    b.fba_fee_cny,
    b.storage_fee_cny,
    b.ad_spend_cny,
    b.return_loss_cny,
    b.fx_effect_cny,
    b.gross_profit_cny,
    b.net_profit_cny,
    CASE WHEN b.net_revenue_cny = 0 THEN NULL
         ELSE b.gross_profit_cny / b.net_revenue_cny END AS gross_margin_pct,
    CASE WHEN b.net_revenue_cny = 0 THEN NULL
         ELSE b.net_profit_cny / b.net_revenue_cny END AS net_margin_pct,
    CASE WHEN b.net_revenue_cny = 0 THEN NULL
         ELSE b.ad_spend_cny / b.net_revenue_cny END AS tacos_pct,
    b.available_qty,
    b.blend AS daily_units_blend,
    CASE WHEN b.blend IS NULL OR b.blend = 0 THEN NULL
         ELSE b.available_qty / b.blend END AS days_of_cover,
    CASE WHEN b.blend IS NULL THEN NULL
         ELSE b.blend * lt.lead_days
              + {{ z_score }} * SQRT(lt.lead_days) * COALESCE(b.sigma30, 0) END AS reorder_point,
    CASE
        WHEN b.available_qty IS NULL THEN NULL
        WHEN b.available_qty <= 0 THEN 'OUT_OF_STOCK'
        WHEN b.blend > 0 AND b.available_qty / b.blend < lt.lead_days THEN 'STOCKOUT_RISK'
        WHEN b.blend > 0 AND b.available_qty / b.blend < lt.lead_days * 1.5 THEN 'LOW'
        WHEN b.blend > 0 AND b.available_qty / b.blend > 180 THEN 'OVERSTOCK'
        ELSE 'HEALTHY'
    END AS stock_status,
    b.fx_missing
FROM (
    SELECT
        w.sku_id,
        w.shop_id,
        w.date_local,
        w.units,
        w.orders,
        w.gmv_local,
        w.gmv_cny,
        w.refund_cny,
        w.net_revenue_cny,
        w.cogs_cny,
        w.referral_fee_cny,
        w.fba_fee_cny,
        w.storage_fee_cny,
        w.ad_spend_cny,
        w.return_loss_cny,
        w.fx_effect_cny,
        w.gross_profit_cny,
        w.net_revenue_cny - w.cogs_cny - w.return_loss_cny
            - w.referral_fee_cny - w.fba_fee_cny - w.storage_fee_cny
            - w.ad_spend_cny + w.fx_effect_cny AS net_profit_cny,
        0.5 * w.avg7 + 0.5 * w.avg30 AS blend,
        w.avg7,
        w.avg30,
        w.sigma30,
        w.available_qty,
        w.category,
        w.fx_missing
    FROM (
        SELECT
            f.sku_id,
            f.shop_id,
            f.date_local,
            f.units,
            f.orders,
            f.gmv_local,
            f.gmv_cny,
            f.refund_cny,
            f.gmv_cny - f.refund_cny AS net_revenue_cny,
            f.units * f.unit_total_cost_cny AS cogs_cny,
            f.referral_fee_cny,
            f.fba_fee_cny,
            f.available_qty * COALESCE(f.unit_cbm, 0)
                * COALESCE(f.storage_rate_per_cbm_month, 0) / 30
                * COALESCE(f.fx_rate, 1) AS storage_fee_cny,
            f.ad_spend_cny,
            f.damaged_units * f.unit_total_cost_cny AS return_loss_cny,
            f.fx_effect_cny,
            (f.gmv_cny - f.refund_cny)
                - f.units * f.unit_total_cost_cny
                - f.damaged_units * f.unit_total_cost_cny AS gross_profit_cny,
            f.category,
            f.available_qty,
            f.fx_missing,
            AVG(f.units) OVER (
                PARTITION BY f.sku_id, f.shop_id
                ORDER BY f.date_local
                ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
            ) AS avg7,
            AVG(f.units) OVER (
                PARTITION BY f.sku_id, f.shop_id
                ORDER BY f.date_local
                ROWS BETWEEN 29 PRECEDING AND CURRENT ROW
            ) AS avg30,
            STDDEV_POP(f.units) OVER (
                PARTITION BY f.sku_id, f.shop_id
                ORDER BY f.date_local
                ROWS BETWEEN 29 PRECEDING AND CURRENT ROW
            ) AS sigma30
        FROM v_daily_fact f
    ) w
) b
JOIN {{ lead_time_union }} lt ON lt.category = b.category;
