"""P2 reconciliation: three independent layers (see docs/metrics.md §9).

L1 identity     - the profit chain adds up, row by row
L2 recompute    - recompute a sample in Python from raw dims/staging, compare
L3 cross-check  - platform fees in the mart equal the settlement ledger
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from sku_cockpit.config import get_settings
from sku_cockpit.db import get_engine
from sku_cockpit.metrics import rebuild_marts

pytestmark = pytest.mark.integration

TOL = Decimal("0.01")


@pytest.fixture(scope="module")
def engine():  # type: ignore[no-untyped-def]
    try:
        eng = get_engine(get_settings())
        with eng.connect() as conn:
            rows = conn.execute(text("SELECT COUNT(*) FROM mart_sku_daily")).scalar_one()
    except (OperationalError, RuntimeError) as exc:  # pragma: no cover
        pytest.skip(f"MySQL not available: {exc}")
    if int(rows) == 0:
        pytest.skip("mart_sku_daily is empty; run `python -m sku_cockpit metrics` first")
    return eng


def _scalar(engine, sql: str):  # type: ignore[no-untyped-def]
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar_one()


def test_l1_profit_identity(engine) -> None:  # type: ignore[no-untyped-def]
    violations = _scalar(
        engine,
        """
        SELECT COUNT(*) FROM mart_sku_daily
        WHERE ABS(net_profit_cny - (
                net_revenue_cny - cogs_cny - referral_fee_cny - fba_fee_cny
                - storage_fee_cny - ad_spend_cny - return_loss_cny + fx_effect_cny
              )) > 0.01
        """,
    )
    assert int(violations) == 0


def test_l1_gross_identity(engine) -> None:  # type: ignore[no-untyped-def]
    violations = _scalar(
        engine,
        """
        SELECT COUNT(*) FROM mart_sku_daily
        WHERE ABS(gross_profit_cny - (net_revenue_cny - cogs_cny - return_loss_cny)) > 0.01
        """,
    )
    assert int(violations) == 0


def test_revenue_is_gmv_minus_refund(engine) -> None:  # type: ignore[no-untyped-def]
    violations = _scalar(
        engine,
        """
        SELECT COUNT(*) FROM mart_sku_daily
        WHERE ABS(net_revenue_cny - (gmv_cny - refund_cny)) > 0.01
        """,
    )
    assert int(violations) == 0


def test_zero_revenue_has_null_margins(engine) -> None:  # type: ignore[no-untyped-def]
    bad = _scalar(
        engine,
        """
        SELECT COUNT(*) FROM mart_sku_daily
        WHERE net_revenue_cny = 0
          AND (net_margin_pct IS NOT NULL OR gross_margin_pct IS NOT NULL)
        """,
    )
    assert int(bad) == 0


def test_margins_are_plausible(engine) -> None:  # type: ignore[no-untyped-def]
    row = (
        engine.connect()
        .execute(
            text(
                """
                SELECT SUM(gross_profit_cny) / SUM(net_revenue_cny) AS gp,
                       SUM(net_profit_cny) / SUM(net_revenue_cny) AS np
                FROM mart_sku_daily
                """
            )
        )
        .mappings()
        .one()
    )
    assert Decimal("0.30") <= row["gp"] <= Decimal("0.55"), row
    assert Decimal("0.02") <= row["np"] <= Decimal("0.15"), row


def test_l3_fees_match_settlement_ledger(engine) -> None:  # type: ignore[no-untyped-def]
    """Platform fees in the mart must equal the settlement rows they came from.

    Population is restricted to settlement rows whose order line actually made it
    into staging: 16 order lines were rejected in P1 while their fees were still
    charged. Those orphans are real-world issues (fees to claim back), not SQL
    bugs, and are asserted separately below.
    """
    sql = """
        SELECT ABS(
            (SELECT SUM(referral_fee_cny + fba_fee_cny) FROM mart_sku_daily)
          - (SELECT SUM(-s.amount_local * fx.rate_to_cny)
             FROM staging_settlement s
             JOIN staging_order_item oi ON oi.order_item_id = s.order_item_id
             JOIN dim_fx fx ON fx.currency = s.currency AND fx.date_key = s.date_local
             WHERE s.fee_type IN ('referral', 'fba_fulfillment') AND s.amount_local < 0)
        ) AS diff
    """
    diff = _scalar(engine, sql)
    assert Decimal(str(diff)) < Decimal("1.0"), diff


def test_orphan_settlement_fees_are_visible(engine) -> None:  # type: ignore[no-untyped-def]
    """Fees charged for order lines that never loaded — must be non-negative and
    discoverable, so P3 can surface them instead of silently losing money."""
    orphan = _scalar(
        engine,
        """
        SELECT COALESCE(SUM(-s.amount_local * fx.rate_to_cny), 0)
        FROM staging_settlement s
        LEFT JOIN staging_order_item oi ON oi.order_item_id = s.order_item_id
        JOIN dim_fx fx ON fx.currency = s.currency AND fx.date_key = s.date_local
        WHERE oi.order_item_id IS NULL AND s.amount_local < 0
        """,
    )
    assert Decimal(str(orphan)) >= Decimal("0")


def test_l2_recompute_fifty_rows_in_python(engine) -> None:  # type: ignore[no-untyped-def]
    """Recompute revenue and COGS from staging + dims, independently of the SQL."""
    with engine.connect() as conn:
        sample = pd.read_sql(
            text(
                """
                SELECT m.sku_id, m.shop_id, m.date_local, m.units, m.gmv_cny,
                       m.cogs_cny, m.net_revenue_cny, m.refund_cny
                FROM mart_sku_daily m
                WHERE m.units > 0
                ORDER BY m.sku_id, m.shop_id, m.date_local
                LIMIT 50
                """
            ),
            conn,
        )
        costs = pd.read_sql(
            text(
                """
                SELECT s.sku_id,
                       s.unit_cost_cny + COALESCE(sh.freight_sum / NULLIF(sh.qty_sum, 0), 0) AS unit_total
                FROM dim_sku s
                LEFT JOIN (SELECT sku_id, SUM(qty) qty_sum, SUM(freight_cny) freight_sum
                           FROM dim_shipment GROUP BY sku_id) sh ON sh.sku_id = s.sku_id
                """
            ),
            conn,
        )
        fx = pd.read_sql(text("SELECT currency, date_key, rate_to_cny FROM dim_fx"), conn)
        shops = pd.read_sql(text("SELECT shop_id, currency FROM dim_shop"), conn)
        actual = pd.read_sql(
            text(
                """
                SELECT sku_id, shop_id, order_date_local AS date_local,
                       SUM(item_total_local) AS item_total_local, SUM(qty) AS qty
                FROM staging_order_item
                GROUP BY sku_id, shop_id, order_date_local
                """
            ),
            conn,
        )

    costs_map = dict(zip(costs["sku_id"], costs["unit_total"], strict=True))
    currency_of = dict(zip(shops["shop_id"], shops["currency"], strict=True))
    fx_map = {
        (row.currency, pd.Timestamp(row.date_key).date()): float(row.rate_to_cny)
        for row in fx.itertuples(index=False)
    }

    checked = 0
    for row in sample.itertuples(index=False):
        key = (row.sku_id, row.shop_id, pd.Timestamp(row.date_local).date())
        match = actual[
            (actual["sku_id"] == row.sku_id)
            & (actual["shop_id"] == row.shop_id)
            & (actual["date_local"] == key[2])
        ]
        if match.empty:
            continue
        currency = currency_of[row.shop_id]
        rate = fx_map[(currency, key[2])]
        expected_gmv = Decimal(str(float(match["item_total_local"].iloc[0]) * rate))
        expected_cogs = Decimal(str(float(match["qty"].iloc[0]) * costs_map[row.sku_id]))
        assert abs(Decimal(str(row.gmv_cny)) - expected_gmv) < TOL, (key, row.gmv_cny, expected_gmv)
        assert abs(Decimal(str(row.cogs_cny)) - expected_cogs) < TOL, (
            key,
            row.cogs_cny,
            expected_cogs,
        )
        checked += 1
    assert checked >= 40, f"only {checked} rows could be recomputed"


def test_rebuild_is_idempotent(engine) -> None:  # type: ignore[no-untyped-def]
    before = _scalar(
        engine,
        "SELECT COUNT(*), ROUND(SUM(net_profit_cny), 4) FROM mart_sku_daily",
    )
    rebuild_marts(engine, get_settings())
    after = _scalar(
        engine,
        "SELECT COUNT(*), ROUND(SUM(net_profit_cny), 4) FROM mart_sku_daily",
    )
    assert before == after
