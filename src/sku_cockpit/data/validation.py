"""Three-tier validation with per-row reason codes.

structural  -> reject the row (unusable)
business    -> reject the row (violates an invariant, computing on it is wrong)
statistical -> keep the row, mark it (an outlier may be the anomaly we hunt)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

REASON_CODES = (
    "STRUCT_NULL",
    "STRUCT_TYPE",
    "STRUCT_RANGE",
    "STRUCT_ENUM",
    "STRUCT_DUP_KEY",
    "BIZ_FK_MISSING",
    "BIZ_CURRENCY",
    "BIZ_QTY_EXCEED",
    "BIZ_TIME_ORDER",
    "BIZ_PRICE_RANGE",
    "MAPPING",
)

FEE_TYPES = ("referral", "fba_fulfillment", "storage", "ads", "refund_admin", "other")
REFUND_REASONS = ("size_mismatch", "defective", "late_delivery", "buyer_remorse", "not_received")
FULFILLMENTS = ("FBA", "FBM")


@dataclass
class Reject:
    row: int
    reason: str
    detail: str
    raw: dict[str, Any]


@dataclass
class Validated:
    dataset: str
    frame: pd.DataFrame
    rejects: list[Reject] = field(default_factory=list)


@dataclass
class Context:
    shop_id: str
    currency: str
    timezone: str
    sku_ids: set[str]
    # order_item_id -> (qty, amount_local, sku_id), used by refund cross-checks
    order_items: dict[str, tuple[int, float, str]] = field(default_factory=dict)


def _parse_datetime(series: pd.Series, formats: tuple[str, ...]) -> pd.Series:
    out = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    for fmt in formats:
        parsed = pd.to_datetime(series, format=fmt, errors="coerce")
        out = out.fillna(parsed)
    return out


def _collect(
    frame: pd.DataFrame,
    mask: pd.Series,
    code: str,
    detail: str,
    rejects: list[Reject],
    rejected: set[int],
) -> None:
    for pos in frame.index[mask]:
        if int(pos) in rejected:
            continue
        rejected.add(int(pos))
        row = frame.loc[pos]
        rejects.append(
            Reject(
                row=int(row["_row"]),
                reason=code,
                detail=detail,
                raw={k: (None if pd.isna(v) else v) for k, v in row.items()},
            )
        )


def _keep(frame: pd.DataFrame, rejected: set[int]) -> pd.DataFrame:
    mask = ~frame.index.isin(list(rejected))
    return frame.loc[mask].copy()


def validate_orders(raw: pd.DataFrame, ctx: Context) -> Validated:
    frame = raw.copy()
    rejects: list[Reject] = []
    rejected: set[int] = set()

    required = [
        "order_item_id",
        "order_id",
        "order_time_local",
        "sku_id",
        "qty",
        "unit_price_local",
        "currency",
    ]
    null_mask = pd.Series(False, index=frame.index)
    for col in required:
        col_null = frame[col].isna() | (frame[col].astype(str).str.strip() == "")
        _collect(frame, col_null, "STRUCT_NULL", f"{col} is empty", rejects, rejected)
        null_mask |= col_null
    frame = _keep(frame, rejected)

    # duplicate primary keys inside the file (D1)
    if not frame.empty:
        dup = frame.duplicated(subset=["order_item_id"], keep="first")
        _collect(
            frame, dup, "STRUCT_DUP_KEY", "order_item_id duplicated in source", rejects, rejected
        )
        frame = _keep(frame, rejected)

    if frame.empty:
        return Validated("orders", frame, rejects)

    qty = pd.to_numeric(frame["qty"], errors="coerce")
    _collect(frame, qty.isna(), "STRUCT_TYPE", "qty is not numeric", rejects, rejected)
    price = pd.to_numeric(frame["unit_price_local"], errors="coerce")
    _collect(frame, price.isna(), "STRUCT_TYPE", "unit_price is not numeric", rejects, rejected)
    stamp = _parse_datetime(frame["order_time_local"], ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"))
    _collect(
        frame, stamp.isna(), "STRUCT_TYPE", "order_time is not a valid date", rejects, rejected
    )
    frame = _keep(frame, rejected)
    if frame.empty:
        return Validated("orders", frame, rejects)

    qty = pd.to_numeric(frame["qty"]).astype("int64")
    price = pd.to_numeric(frame["unit_price_local"]).astype("float64")
    stamp = _parse_datetime(frame["order_time_local"], ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"))
    _collect(frame, qty <= 0, "STRUCT_RANGE", "qty must be > 0", rejects, rejected)
    _collect(frame, price <= 0, "STRUCT_RANGE", "unit_price must be > 0", rejects, rejected)
    _collect(
        frame,
        frame["currency"].astype(str).str.upper() != ctx.currency,
        "BIZ_CURRENCY",
        f"currency must be {ctx.currency}",
        rejects,
        rejected,
    )
    _collect(
        frame,
        ~frame["sku_id"].astype(str).isin(ctx.sku_ids),
        "BIZ_FK_MISSING",
        "sku_id not found in dim_sku",
        rejects,
        rejected,
    )
    frame = _keep(frame, rejected)
    if frame.empty:
        return Validated("orders", frame, rejects)

    qty = pd.to_numeric(frame["qty"]).astype("int64")
    price = pd.to_numeric(frame["unit_price_local"]).astype("float64")
    stamp = _parse_datetime(frame["order_time_local"], ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"))

    frame["qty"] = qty
    frame["unit_price_local"] = price.round(4)
    frame["item_total_local"] = (qty * price).round(4)
    frame["order_total_local"] = frame["item_total_local"]
    frame["order_at_utc"] = stamp
    frame["order_date_local"] = stamp.dt.date
    frame["currency"] = frame["currency"].astype(str).str.upper()
    frame["fulfillment"] = frame["fulfillment"].fillna("FBA").astype(str)
    _collect(
        frame,
        ~frame["fulfillment"].isin(FULFILLMENTS),
        "STRUCT_ENUM",
        "fulfillment must be FBA or FBM",
        rejects,
        rejected,
    )
    frame = _keep(frame, rejected)
    return Validated("orders", frame, rejects)


def validate_refunds(raw: pd.DataFrame, ctx: Context) -> Validated:
    frame = raw.copy()
    rejects: list[Reject] = []
    rejected: set[int] = set()

    required = [
        "refund_id",
        "order_item_id",
        "sku_id",
        "qty",
        "amount_local",
        "currency",
        "reason_code",
        "refund_date_local",
    ]
    for col in required:
        _collect(
            frame,
            frame[col].isna(),
            "STRUCT_NULL",
            f"{col} is empty",
            rejects,
            rejected,
        )
    frame = _keep(frame, rejected)
    if frame.empty:
        return Validated("refunds", frame, rejects)

    qty = pd.to_numeric(frame["qty"], errors="coerce")
    amount = pd.to_numeric(frame["amount_local"], errors="coerce")
    when = _parse_datetime(frame["refund_date_local"], ("%Y-%m-%d",))
    _collect(frame, qty.isna(), "STRUCT_TYPE", "qty is not numeric", rejects, rejected)
    _collect(frame, amount.isna(), "STRUCT_TYPE", "refund amount is not numeric", rejects, rejected)
    _collect(frame, when.isna(), "STRUCT_TYPE", "refund date invalid", rejects, rejected)
    frame = _keep(frame, rejected)
    if frame.empty:
        return Validated("refunds", frame, rejects)

    qty = pd.to_numeric(frame["qty"]).astype("int64")
    amount = pd.to_numeric(frame["amount_local"]).astype("float64")
    when = _parse_datetime(frame["refund_date_local"], ("%Y-%m-%d",))
    _collect(frame, qty <= 0, "STRUCT_RANGE", "refund qty must be > 0", rejects, rejected)
    _collect(frame, amount <= 0, "STRUCT_RANGE", "refund amount must be > 0", rejects, rejected)
    _collect(
        frame,
        frame["currency"].astype(str).str.upper() != ctx.currency,
        "BIZ_CURRENCY",
        f"currency must be {ctx.currency}",
        rejects,
        rejected,
    )
    _collect(
        frame,
        ~frame["reason_code"].astype(str).isin(REFUND_REASONS),
        "STRUCT_ENUM",
        "unknown refund reason",
        rejects,
        rejected,
    )

    # cross-check against the order line (B3)
    unknown = []
    over = []
    for pos in frame.index:
        item_id = str(frame.at[pos, "order_item_id"])
        info = ctx.order_items.get(item_id)
        if info is None:
            unknown.append(pos)
        elif qty.at[pos] > info[0]:
            over.append(pos)
    if unknown:
        _collect(
            frame,
            pd.Series(frame.index.isin(unknown), index=frame.index),
            "BIZ_FK_MISSING",
            "order_item_id not found in orders",
            rejects,
            rejected,
        )
    if over:
        _collect(
            frame,
            pd.Series(frame.index.isin(over), index=frame.index),
            "BIZ_QTY_EXCEED",
            "refund qty exceeds ordered qty",
            rejects,
            rejected,
        )
    frame = _keep(frame, rejected)
    if frame.empty:
        return Validated("refunds", frame, rejects)

    frame["qty"] = pd.to_numeric(frame["qty"]).astype("int64")
    frame["amount_local"] = pd.to_numeric(frame["amount_local"]).round(4)
    frame["refund_at_utc"] = _parse_datetime(frame["refund_date_local"], ("%Y-%m-%d",))
    frame["refund_date_local"] = frame["refund_at_utc"].dt.date
    return Validated("refunds", frame, rejects)


def validate_ads(raw: pd.DataFrame, ctx: Context) -> Validated:
    frame = raw.copy()
    rejects: list[Reject] = []
    rejected: set[int] = set()
    for col in ("campaign_id", "sku_id", "date_local", "impressions", "clicks", "spend_local"):
        _collect(frame, frame[col].isna(), "STRUCT_NULL", f"{col} is empty", rejects, rejected)
    frame = _keep(frame, rejected)
    if frame.empty:
        return Validated("ads", frame, rejects)

    when = _parse_datetime(frame["date_local"], ("%Y-%m-%d",))
    _collect(frame, when.isna(), "STRUCT_TYPE", "ad date invalid", rejects, rejected)
    spend = pd.to_numeric(frame["spend_local"], errors="coerce")
    _collect(frame, spend.isna(), "STRUCT_TYPE", "spend is not numeric", rejects, rejected)
    _collect(frame, spend.fillna(0) < 0, "STRUCT_RANGE", "spend must be >= 0", rejects, rejected)
    _collect(
        frame,
        ~frame["sku_id"].astype(str).isin(ctx.sku_ids),
        "BIZ_FK_MISSING",
        "sku_id not found in dim_sku",
        rejects,
        rejected,
    )
    frame = _keep(frame, rejected)
    if frame.empty:
        return Validated("ads", frame, rejects)

    frame["date_local"] = _parse_datetime(frame["date_local"], ("%Y-%m-%d",)).dt.date
    # the export has no ad row id; derive a stable one
    frame["ad_spend_id"] = (
        frame["campaign_id"].astype(str)
        + "-"
        + frame["sku_id"].astype(str)
        + "-"
        + frame["date_local"].astype(str)
    )
    for col in ("impressions", "clicks"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0).astype("int64")
    frame["spend_local"] = pd.to_numeric(frame["spend_local"]).round(4)
    frame["attributed_sales_local"] = (
        pd.to_numeric(frame["attributed_sales_local"], errors="coerce").fillna(0).round(4)
    )
    return Validated("ads", frame, rejects)


def validate_settlement(raw: pd.DataFrame, ctx: Context) -> Validated:
    frame = raw.copy()
    rejects: list[Reject] = []
    rejected: set[int] = set()
    for col in ("settlement_id", "date_local", "fee_type", "amount_local", "currency"):
        _collect(frame, frame[col].isna(), "STRUCT_NULL", f"{col} is empty", rejects, rejected)
    frame = _keep(frame, rejected)
    if frame.empty:
        return Validated("settlement", frame, rejects)

    when = _parse_datetime(frame["date_local"], ("%Y-%m-%d",))
    _collect(frame, when.isna(), "STRUCT_TYPE", "settlement date invalid", rejects, rejected)
    amount = pd.to_numeric(frame["amount_local"], errors="coerce")
    _collect(frame, amount.isna(), "STRUCT_TYPE", "amount is not numeric", rejects, rejected)
    _collect(
        frame, amount.fillna(0) > 0, "STRUCT_RANGE", "fee amount must be <= 0", rejects, rejected
    )
    _collect(
        frame,
        ~frame["fee_type"].astype(str).isin(FEE_TYPES),
        "STRUCT_ENUM",
        "unknown fee_type",
        rejects,
        rejected,
    )
    _collect(
        frame,
        frame["currency"].astype(str).str.upper() != ctx.currency,
        "BIZ_CURRENCY",
        f"currency must be {ctx.currency}",
        rejects,
        rejected,
    )
    frame = _keep(frame, rejected)
    if frame.empty:
        return Validated("settlement", frame, rejects)

    frame["date_local"] = _parse_datetime(frame["date_local"], ("%Y-%m-%d",)).dt.date
    frame["amount_local"] = pd.to_numeric(frame["amount_local"]).round(4)
    return Validated("settlement", frame, rejects)


def validate_inventory(raw: pd.DataFrame, ctx: Context) -> Validated:
    frame = raw.copy()
    rejects: list[Reject] = []
    rejected: set[int] = set()
    for col in ("snapshot_id", "sku_id", "warehouse", "date_local", "available_qty"):
        _collect(frame, frame[col].isna(), "STRUCT_NULL", f"{col} is empty", rejects, rejected)
    frame = _keep(frame, rejected)
    if frame.empty:
        return Validated("inventory", frame, rejects)

    when = _parse_datetime(frame["date_local"], ("%Y-%m-%d",))
    _collect(frame, when.isna(), "STRUCT_TYPE", "inventory date invalid", rejects, rejected)
    avail = pd.to_numeric(frame["available_qty"], errors="coerce")
    _collect(frame, avail.isna(), "STRUCT_TYPE", "available_qty is not numeric", rejects, rejected)
    _collect(frame, avail.fillna(0) < 0, "STRUCT_RANGE", "available_qty < 0", rejects, rejected)
    _collect(
        frame,
        ~frame["sku_id"].astype(str).isin(ctx.sku_ids),
        "BIZ_FK_MISSING",
        "sku_id not found in dim_sku",
        rejects,
        rejected,
    )
    frame = _keep(frame, rejected)
    if frame.empty:
        return Validated("inventory", frame, rejects)

    frame["date_local"] = _parse_datetime(frame["date_local"], ("%Y-%m-%d",)).dt.date
    for col in ("available_qty", "inbound_qty", "reserved_qty"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0).astype("int64")
    return Validated("inventory", frame, rejects)


VALIDATORS = {
    "orders": validate_orders,
    "refunds": validate_refunds,
    "ads": validate_ads,
    "settlement": validate_settlement,
    "inventory": validate_inventory,
}


def statistical_flags(frame: pd.DataFrame, column: str, z_threshold: float = 5.0) -> pd.Series:
    """Keep the row, but mark it. Outliers may be exactly what we want to find."""
    values = pd.to_numeric(frame[column], errors="coerce")
    mean = float(np.nanmean(values))
    std = float(np.nanstd(values)) or 1.0
    return (values - mean).abs() > z_threshold * std


def parse_utc(stamp: datetime) -> datetime:
    return stamp
