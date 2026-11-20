"""Database schema (SQLAlchemy Core).

Layer convention:
    raw_*      untouched landing zone (one row per source row, JSON payload)
    staging_*  parsed & validated, typed columns
    dim_*      dimensions
    mart_*     business marts (built in P2)

Money is always NUMERIC(18, 4) with an explicit currency column next to it.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Table,
    UniqueConstraint,
    func,
)

metadata = MetaData()

MONEY = Numeric(18, 4)
RATE = Numeric(12, 6)

# --------------------------------------------------------------------- dims
dim_shop = Table(
    "dim_shop",
    metadata,
    Column("shop_id", String(10), primary_key=True),
    Column("shop_name", String(64), nullable=False),
    Column("marketplace", String(8), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("timezone", String(40), nullable=False),
    Column("status", String(16), nullable=False, default="active"),
)

dim_sku = Table(
    "dim_sku",
    metadata,
    Column("sku_id", String(24), primary_key=True),
    Column("parent_asin", String(16), nullable=True),
    Column("asin", String(16), nullable=False),
    Column("title", String(255), nullable=False),
    Column("brand", String(64), nullable=False),
    Column("category", String(40), nullable=False),
    Column("product_type", String(40), nullable=False),
    Column("unit_cost_cny", MONEY, nullable=False),
    Column("unit_cbm", Numeric(12, 6), nullable=False, default=0),
    Column("launch_date", Date, nullable=False),
    Column("lifecycle", String(16), nullable=False),
    Index("ix_dim_sku_category", "category"),
)

dim_listing = Table(
    "dim_listing",
    metadata,
    Column("listing_id", String(32), primary_key=True),
    Column("sku_id", String(24), nullable=False),
    Column("shop_id", String(10), nullable=False),
    Column("asin", String(16), nullable=False),
    Column("title", String(255), nullable=False),
    Column("price_local", MONEY, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("status", String(16), nullable=False),
    UniqueConstraint("shop_id", "asin", name="uq_listing_shop_asin"),
    Index("ix_dim_listing_sku", "sku_id"),
)

dim_shipment = Table(
    "dim_shipment",
    metadata,
    Column("shipment_id", String(24), primary_key=True),
    Column("sku_id", String(24), nullable=False),
    Column("qty", Integer, nullable=False),
    Column("freight_cny", MONEY, nullable=False),
    Column("arrived_at", Date, nullable=False),
    Column("warehouse", String(24), nullable=False),
)

dim_fee_schedule = Table(
    "dim_fee_schedule",
    metadata,
    Column("fee_id", Integer, primary_key=True, autoincrement=True),
    Column("category", String(40), nullable=False),
    Column("marketplace", String(8), nullable=False),
    Column("referral_rate", RATE, nullable=False),
    Column("referral_min", MONEY, nullable=False),
    Column("fba_size_tier", String(16), nullable=False),
    Column("fba_weight_tier", String(16), nullable=False),
    Column("fba_fee_local", MONEY, nullable=False),
    Column("storage_rate_per_cbm_month", MONEY, nullable=False),
    Column("effective_from", Date, nullable=False),
    Column("effective_to", Date, nullable=True),
    UniqueConstraint(
        "category",
        "marketplace",
        "fba_size_tier",
        "fba_weight_tier",
        "effective_from",
        name="uq_fee_schedule_version",
    ),
)

dim_date = Table(
    "dim_date",
    metadata,
    Column("date_key", Date, primary_key=True),
    Column("year", SmallInteger, nullable=False),
    Column("month", SmallInteger, nullable=False),
    Column("week", SmallInteger, nullable=False),
    Column("weekday", SmallInteger, nullable=False),
    Column("is_promo", Boolean, nullable=False, default=False),
    Column("promo_name", String(32), nullable=True),
)

dim_fx = Table(
    "dim_fx",
    metadata,
    Column("currency", String(3), primary_key=True),
    Column("date_key", Date, primary_key=True),
    Column("rate_to_cny", Numeric(12, 6), nullable=False),
)

# ------------------------------------------------------------------- marts
mart_sku_daily = Table(
    "mart_sku_daily",
    metadata,
    Column("sku_id", String(24), primary_key=True),
    Column("shop_id", String(10), primary_key=True),
    Column("date_local", Date, primary_key=True),
    Column("units", Integer, nullable=False, default=0),
    Column("orders", Integer, nullable=False, default=0),
    Column("gmv_local", MONEY, nullable=False, default=0),
    Column("gmv_cny", MONEY, nullable=False, default=0),
    Column("refund_cny", MONEY, nullable=False, default=0),
    Column("net_revenue_cny", MONEY, nullable=False, default=0),
    Column("cogs_cny", MONEY, nullable=False, default=0),
    Column("referral_fee_cny", MONEY, nullable=False, default=0),
    Column("fba_fee_cny", MONEY, nullable=False, default=0),
    Column("storage_fee_cny", MONEY, nullable=False, default=0),
    Column("ad_spend_cny", MONEY, nullable=False, default=0),
    Column("return_loss_cny", MONEY, nullable=False, default=0),
    Column("fx_effect_cny", MONEY, nullable=False, default=0),
    Column("gross_profit_cny", MONEY, nullable=False, default=0),
    Column("net_profit_cny", MONEY, nullable=False, default=0),
    Column("gross_margin_pct", Numeric(9, 6), nullable=True),
    Column("net_margin_pct", Numeric(9, 6), nullable=True),
    Column("tacos_pct", Numeric(9, 6), nullable=True),
    Column("available_qty", Integer, nullable=True),
    Column("daily_units_blend", Numeric(12, 4), nullable=True),
    Column("days_of_cover", Numeric(12, 2), nullable=True),
    Column("reorder_point", Numeric(12, 2), nullable=True),
    Column("stock_status", String(16), nullable=True),
    Column("fx_missing", Boolean, nullable=False, default=False),
    Index("ix_mart_sku_daily_date", "date_local"),
    Index("ix_mart_sku_daily_sku", "sku_id", "date_local"),
)

mart_sku_monthly = Table(
    "mart_sku_monthly",
    metadata,
    Column("sku_id", String(24), primary_key=True),
    Column("shop_id", String(10), primary_key=True),
    Column("month", Date, primary_key=True),
    Column("units", Integer, nullable=False, default=0),
    Column("orders", Integer, nullable=False, default=0),
    Column("gmv_cny", MONEY, nullable=False, default=0),
    Column("refund_cny", MONEY, nullable=False, default=0),
    Column("net_revenue_cny", MONEY, nullable=False, default=0),
    Column("cogs_cny", MONEY, nullable=False, default=0),
    Column("fee_total_cny", MONEY, nullable=False, default=0),
    Column("ad_spend_cny", MONEY, nullable=False, default=0),
    Column("gross_profit_cny", MONEY, nullable=False, default=0),
    Column("net_profit_cny", MONEY, nullable=False, default=0),
    Column("gross_margin_pct", Numeric(9, 6), nullable=True),
    Column("net_margin_pct", Numeric(9, 6), nullable=True),
    Column("tacos_pct", Numeric(9, 6), nullable=True),
)

mart_shop_kpi = Table(
    "mart_shop_kpi",
    metadata,
    Column("shop_id", String(10), primary_key=True),
    Column("month", Date, primary_key=True),
    Column("gmv_cny", MONEY, nullable=False, default=0),
    Column("net_revenue_cny", MONEY, nullable=False, default=0),
    Column("net_profit_cny", MONEY, nullable=False, default=0),
    Column("net_margin_pct", Numeric(9, 6), nullable=True),
    Column("orders", Integer, nullable=False, default=0),
    Column("units", Integer, nullable=False, default=0),
    Column("aov_cny", MONEY, nullable=True),
    Column("tacos_pct", Numeric(9, 6), nullable=True),
    Column("sku_count", Integer, nullable=False, default=0),
    Column("risk_sku_count", Integer, nullable=False, default=0),
)

# ------------------------------------------------------------------ control
ingest_run = Table(
    "ingest_run",
    metadata,
    Column("run_id", BigInteger, primary_key=True, autoincrement=True),
    Column("source_file", String(255), nullable=False),
    Column("file_hash", String(64), nullable=False),
    Column("dataset", String(32), nullable=False),
    Column("shop_id", String(10), nullable=True),
    Column("rows_total", Integer, nullable=False, default=0),
    Column("rows_loaded", Integer, nullable=False, default=0),
    Column("rows_rejected", Integer, nullable=False, default=0),
    Column("started_at", DateTime, nullable=False, server_default=func.now()),
    Column("finished_at", DateTime, nullable=True),
    Column("status", String(16), nullable=False, default="running"),
    UniqueConstraint("file_hash", "dataset", name="uq_ingest_file_dataset"),
)

raw_row = Table(
    "raw_row",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("run_id", BigInteger, nullable=False),
    Column("dataset", String(32), nullable=False),
    Column("row_number", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
    Index("ix_raw_row_run", "run_id"),
)

reject_row = Table(
    "reject_row",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("run_id", BigInteger, nullable=False),
    Column("row_number", Integer, nullable=False),
    Column("reason_code", String(32), nullable=False),
    Column("detail", String(255), nullable=True),
    Column("raw_row_json", JSON, nullable=False),
    Index("ix_reject_row_run", "run_id"),
    Index("ix_reject_row_reason", "reason_code"),
)

anomaly_label = Table(
    "anomaly_label",
    metadata,
    Column("label_id", BigInteger, primary_key=True, autoincrement=True),
    Column("sku_id", String(24), nullable=False),
    Column("shop_id", String(10), nullable=False),
    Column("date_from", Date, nullable=False),
    Column("date_to", Date, nullable=False),
    Column("anomaly_type", String(32), nullable=False),
    Column("is_anomaly", Boolean, nullable=False, default=True),
    Column("source", String(16), nullable=False, default="injected"),
    Column("note", String(255), nullable=True),
    Index("ix_anomaly_label_sku", "sku_id", "shop_id"),
)

# ------------------------------------------------------------------ staging
staging_order_item = Table(
    "staging_order_item",
    metadata,
    Column("order_item_id", String(48), primary_key=True),
    Column("order_id", String(48), nullable=False),
    Column("shop_id", String(10), nullable=False),
    Column("sku_id", String(24), nullable=False),
    Column("listing_id", String(32), nullable=True),
    Column("qty", Integer, nullable=False),
    Column("unit_price_local", MONEY, nullable=False),
    Column("item_total_local", MONEY, nullable=False),
    Column("order_total_local", MONEY, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("order_at_utc", DateTime, nullable=False),
    Column("order_date_local", Date, nullable=False),
    Column("fulfillment", String(8), nullable=False),
    Column("status", String(16), nullable=False),
    Index("ix_stg_order_sku_date", "sku_id", "order_date_local"),
    Index("ix_stg_order_shop_date", "shop_id", "order_date_local"),
)

staging_refund = Table(
    "staging_refund",
    metadata,
    Column("refund_id", String(48), primary_key=True),
    Column("order_item_id", String(48), nullable=False),
    Column("shop_id", String(10), nullable=False),
    Column("sku_id", String(24), nullable=False),
    Column("qty", Integer, nullable=False),
    Column("amount_local", MONEY, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("reason_code", String(32), nullable=False),
    Column("refund_at_utc", DateTime, nullable=False),
    Column("refund_date_local", Date, nullable=False),
    Column("restockable", Boolean, nullable=False),
    Index("ix_stg_refund_sku", "sku_id"),
)

staging_ad_spend = Table(
    "staging_ad_spend",
    metadata,
    Column("ad_spend_id", String(48), primary_key=True),
    Column("shop_id", String(10), nullable=False),
    Column("sku_id", String(24), nullable=True),
    Column("campaign_id", String(32), nullable=False),
    Column("campaign_type", String(8), nullable=False),
    Column("date_local", Date, nullable=False),
    Column("impressions", Integer, nullable=False),
    Column("clicks", Integer, nullable=False),
    Column("spend_local", MONEY, nullable=False),
    Column("attributed_sales_local", MONEY, nullable=False),
    Column("currency", String(3), nullable=False),
    Index("ix_stg_ads_sku_date", "sku_id", "date_local"),
)

staging_inventory_snapshot = Table(
    "staging_inventory_snapshot",
    metadata,
    Column("snapshot_id", String(48), primary_key=True),
    Column("shop_id", String(10), nullable=False),
    Column("sku_id", String(24), nullable=False),
    Column("warehouse", String(24), nullable=False),
    Column("date_local", Date, nullable=False),
    Column("available_qty", Integer, nullable=False),
    Column("inbound_qty", Integer, nullable=False),
    Column("reserved_qty", Integer, nullable=False),
    UniqueConstraint("shop_id", "sku_id", "warehouse", "date_local", name="uq_inv_snapshot"),
)

staging_settlement = Table(
    "staging_settlement",
    metadata,
    Column("settlement_id", String(48), primary_key=True),
    Column("shop_id", String(10), nullable=False),
    Column("order_item_id", String(48), nullable=True),
    Column("date_local", Date, nullable=False),
    Column("fee_type", String(24), nullable=False),
    Column("amount_local", MONEY, nullable=False),
    Column("currency", String(3), nullable=False),
    Index("ix_stg_settle_shop_date", "shop_id", "date_local"),
)

STAGING_TABLES = {
    "orders": staging_order_item,
    "refunds": staging_refund,
    "ads": staging_ad_spend,
    "inventory": staging_inventory_snapshot,
    "settlement": staging_settlement,
}

# traceability: every staging row points back at the run and source line
for _table in STAGING_TABLES.values():
    _table.append_column(Column("source_run_id", BigInteger, nullable=True))
    _table.append_column(Column("source_row", Integer, nullable=True))
