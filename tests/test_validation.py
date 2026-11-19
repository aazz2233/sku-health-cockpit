from __future__ import annotations

import pandas as pd

from sku_cockpit.data.validation import (
    Context,
    validate_ads,
    validate_orders,
    validate_refunds,
    validate_settlement,
)


def _ctx() -> Context:
    return Context(
        shop_id="US01",
        currency="USD",
        timezone="America/Los_Angeles",
        sku_ids={"SKU-0001", "SKU-0002"},
        order_items={"A-1": (2, 20.0, "SKU-0001")},
    )


def _orders(**overrides: object) -> pd.DataFrame:
    base: dict[str, list[object]] = {
        "order_item_id": ["A-1"],
        "order_id": ["A"],
        "order_time_local": ["2024-06-01 10:00:00"],
        "sku_id": ["SKU-0001"],
        "qty": ["2"],
        "unit_price_local": ["10.00"],
        "currency": ["USD"],
        "fulfillment": ["FBA"],
        "status": ["Shipped"],
        "_row": [1],
    }
    base.update({k: [v] for k, v in overrides.items()})
    return pd.DataFrame(base)


def test_valid_order_passes_and_is_typed() -> None:
    result = validate_orders(_orders(), _ctx())
    assert result.rejects == []
    assert len(result.frame) == 1
    row = result.frame.iloc[0]
    assert int(row["qty"]) == 2
    assert float(row["item_total_local"]) == 20.0


def test_negative_quantity_is_rejected() -> None:
    result = validate_orders(_orders(qty="-1"), _ctx())
    assert [r.reason for r in result.rejects] == ["STRUCT_RANGE"]


def test_missing_price_is_rejected() -> None:
    result = validate_orders(_orders(unit_price_local=None), _ctx())
    assert [r.reason for r in result.rejects] == ["STRUCT_NULL"]


def test_bad_date_is_rejected() -> None:
    result = validate_orders(_orders(order_time_local="2024-02-30 10:00:00"), _ctx())
    assert [r.reason for r in result.rejects] == ["STRUCT_TYPE"]


def test_currency_mismatch_is_rejected() -> None:
    result = validate_orders(_orders(currency="EUR"), _ctx())
    assert [r.reason for r in result.rejects] == ["BIZ_CURRENCY"]


def test_unknown_sku_is_rejected() -> None:
    result = validate_orders(_orders(sku_id="SKU-9999"), _ctx())
    assert [r.reason for r in result.rejects] == ["BIZ_FK_MISSING"]


def test_duplicate_primary_key_keeps_first() -> None:
    frame = _orders()
    duplicated = pd.concat([frame, frame], ignore_index=True)
    duplicated["_row"] = [1, 2]
    result = validate_orders(duplicated, _ctx())
    assert [r.reason for r in result.rejects] == ["STRUCT_DUP_KEY"]
    assert len(result.frame) == 1


def test_refund_exceeding_order_quantity_is_rejected() -> None:
    frame = pd.DataFrame(
        {
            "refund_id": ["R-1"],
            "order_item_id": ["A-1"],
            "sku_id": ["SKU-0001"],
            "qty": ["5"],
            "amount_local": ["9.50"],
            "currency": ["USD"],
            "reason_code": ["defective"],
            "refund_date_local": ["2024-06-10"],
            "_row": [1],
        }
    )
    result = validate_refunds(frame, _ctx())
    assert [r.reason for r in result.rejects] == ["BIZ_QTY_EXCEED"]


def test_refund_for_unknown_order_line_is_rejected() -> None:
    frame = pd.DataFrame(
        {
            "refund_id": ["R-1"],
            "order_item_id": ["NOPE"],
            "sku_id": ["SKU-0001"],
            "qty": ["1"],
            "amount_local": ["9.50"],
            "currency": ["USD"],
            "reason_code": ["defective"],
            "refund_date_local": ["2024-06-10"],
            "_row": [1],
        }
    )
    result = validate_refunds(frame, _ctx())
    assert [r.reason for r in result.rejects] == ["BIZ_FK_MISSING"]


def test_positive_settlement_amount_is_rejected() -> None:
    frame = pd.DataFrame(
        {
            "settlement_id": ["S-1"],
            "order_item_id": ["A-1"],
            "date_local": ["2024-06-01"],
            "fee_type": ["referral"],
            "amount_local": ["3.00"],
            "currency": ["USD"],
            "_row": [1],
        }
    )
    result = validate_settlement(frame, _ctx())
    assert [r.reason for r in result.rejects] == ["STRUCT_RANGE"]


def test_ads_rows_get_a_derived_id() -> None:
    frame = pd.DataFrame(
        {
            "campaign_id": ["C-1"],
            "sku_id": ["SKU-0001"],
            "date_local": ["2024-06-01"],
            "impressions": ["1000"],
            "clicks": ["20"],
            "spend_local": ["4.5"],
            "attributed_sales_local": ["40"],
            "currency": ["USD"],
            "_row": [1],
        }
    )
    result = validate_ads(frame, _ctx())
    assert result.rejects == []
    assert result.frame.iloc[0]["ad_spend_id"] == "C-1-SKU-0001-2024-06-01"
