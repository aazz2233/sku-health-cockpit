"""Unit tests for the anomaly engine (no database needed)."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest
import yaml

from sku_cockpit.anomaly import (
    ROOT,
    detect,
    evaluate,
    r1_negative_margin,
    r4_sales_cliff,
    r5_refund_spike,
    r7_overstock,
    r9_variant_price,
)

CFG = yaml.safe_load((ROOT / "config" / "rules.yaml").read_text(encoding="utf-8"))

DEFAULTS: dict[str, object] = {
    "units": 0,
    "orders": 0,
    "gmv_cny": 0.0,
    "refund_cny": 0.0,
    "net_revenue_cny": 0.0,
    "cogs_cny": 0.0,
    "referral_fee_cny": 0.0,
    "fba_fee_cny": 0.0,
    "storage_fee_cny": 0.0,
    "ad_spend_cny": 0.0,
    "return_loss_cny": 0.0,
    "fx_effect_cny": 0.0,
    "gross_profit_cny": 0.0,
    "net_profit_cny": 0.0,
    "gross_margin_pct": None,
    "net_margin_pct": None,
    "tacos_pct": None,
    "available_qty": None,
    "daily_units_blend": None,
    "days_of_cover": None,
    "reorder_point": None,
    "stock_status": "HEALTHY",
    "fx_missing": False,
    "category": "home",
}


def frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for col, default in DEFAULTS.items():
        if col not in df.columns:
            df[col] = default
    df["date_local"] = pd.to_datetime(df["date_local"]).dt.date
    return df


def series(
    days: int,
    sku: str = "SKU-0001",
    shop: str = "US01",
    **overrides: object,
) -> list[dict[str, object]]:
    start = date(2024, 6, 1)
    rows = []
    for i in range(days):
        row: dict[str, object] = {
            "sku_id": sku,
            "shop_id": shop,
            "date_local": start + timedelta(days=i),
        }
        row.update(overrides)
        rows.append(row)
    return rows


def test_r1_fires_when_margin_deteriorates() -> None:
    rows = []
    for i, r in enumerate(series(60, net_revenue_cny=2000.0)):
        profit = 200.0 if i < 35 else -600.0
        rows.append({**r, "net_profit_cny": profit})
    events = r1_negative_margin(frame(rows), CFG)
    assert len(events) == 1
    assert events[0]["rule_id"] == "R1"
    assert events[0]["evidence"]["margin_7d"] < -0.04


def test_r1_ignores_consistently_thin_sku() -> None:
    """A SKU that is always thin is not an anomaly — no deterioration, no event."""
    rows = [{**r, "net_profit_cny": -200.0} for r in series(60, net_revenue_cny=2000.0)]
    assert r1_negative_margin(frame(rows), CFG) == []


def test_r4_detects_sales_cliff() -> None:
    rows = []
    for i, r in enumerate(series(50, units=10)):
        rows.append({**r, "units": 10 if i < 35 else 1})
    events = r4_sales_cliff(frame(rows), CFG)
    assert len(events) == 1
    assert events[0]["rule_id"] == "R4"


def test_r7_detects_inbound_jump_only() -> None:
    rows = []
    for i, r in enumerate(series(20, available_qty=100)):
        rows.append({**r, "available_qty": 100 if i < 10 else 3000})
    assert len(r7_overstock(frame(rows), CFG)) == 1
    flat = [{**r, "available_qty": 100} for r in series(20)]
    assert r7_overstock(frame(flat), CFG) == []


def test_r5_fires_on_category_outlier() -> None:
    skus = pd.DataFrame(
        [
            {"sku_id": "SKU-0001", "category": "home", "parent_asin": "P1"},
            {"sku_id": "SKU-0002", "category": "home", "parent_asin": "P2"},
        ]
    )
    rows = []
    for r in series(40, sku="SKU-0001"):
        rows.append({**r, "gmv_cny": 2000.0, "refund_cny": 600.0})  # 30% refund rate
    for r in series(40, sku="SKU-0002"):
        rows.append({**r, "gmv_cny": 4000.0, "refund_cny": 40.0})  # 1% refund rate
    events = r5_refund_spike(frame(rows), skus, CFG)
    assert any(e["sku_id"] == "SKU-0001" for e in events)
    assert not any(e["sku_id"] == "SKU-0002" for e in events)


def test_r9_flags_expensive_variant() -> None:
    skus = pd.DataFrame(
        [
            {"sku_id": "A", "parent_asin": "P1"},
            {"sku_id": "B", "parent_asin": "P1"},
            {"sku_id": "C", "parent_asin": "P1"},
        ]
    )
    listings = pd.DataFrame(
        [
            {
                "listing_id": "L1",
                "sku_id": "A",
                "shop_id": "US01",
                "title": "a",
                "price_local": 100.0,
            },
            {
                "listing_id": "L2",
                "sku_id": "B",
                "shop_id": "US01",
                "title": "b",
                "price_local": 105.0,
            },
            {
                "listing_id": "L3",
                "sku_id": "C",
                "shop_id": "US01",
                "title": "c",
                "price_local": 150.0,
            },
        ]
    )
    events = r9_variant_price(listings, skus, CFG)
    assert len(events) == 1
    assert events[0]["sku_id"] == "C"


def test_evaluate_matches_datelss_events() -> None:
    events = pd.DataFrame(
        [
            {
                "rule_id": "R9",
                "sku_id": "C",
                "shop_id": "US01",
                "date_local": None,
                "severity": "P2",
                "evidence": {},
            }
        ]
    )
    labels = pd.DataFrame(
        [
            {
                "sku_id": "C",
                "shop_id": "US01",
                "date_from": date(2024, 6, 1),
                "date_to": date(2024, 8, 29),
                "anomaly_type": "variant_price",
            }
        ]
    )
    result = evaluate(events, labels)
    row = result[result["rule"] == "R9"].iloc[0]
    assert row["recall"] == pytest.approx(1.0)


def test_detect_runs_on_small_valid_input() -> None:
    df = frame(series(5, net_revenue_cny=100.0, net_profit_cny=10.0))
    skus = pd.DataFrame([{"sku_id": "SKU-0001", "parent_asin": "P1", "category": "home"}])
    listings = pd.DataFrame(
        [
            {
                "listing_id": "L1",
                "sku_id": "SKU-0001",
                "shop_id": "US01",
                "title": "t",
                "price_local": 10.0,
            }
        ]
    )
    out = detect(df, skus, listings, CFG)
    assert isinstance(out, pd.DataFrame)
