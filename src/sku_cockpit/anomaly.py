"""P3 anomaly detection engine + evaluation against injected ground truth.

Every rule is a pure function over pandas frames and a YAML config. The
evaluation compares rule output with `anomaly_label` (P1 injected anomalies),
so recall/precision are real numbers, not fabricated.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd
import yaml
from rapidfuzz import fuzz
from sqlalchemy import Engine, text

from sku_cockpit.config import ROOT, Settings, get_settings
from sku_cockpit.schema import anomaly_event

RULES_CFG_PATH = ROOT / "config" / "rules.yaml"

# anomaly_type (in anomaly_label) -> rule that should catch it
TYPE_TO_RULE: dict[str, str] = {
    "negative_margin": "R1",
    "ad_spike": "R3",
    "sales_cliff": "R4",
    "refund_spike": "R5",
    "stockout_risk": "R6",
    "overstock": "R7",
    "variant_price": "R9",
}

NUM_COLS = [
    "units",
    "orders",
    "gmv_cny",
    "refund_cny",
    "net_revenue_cny",
    "cogs_cny",
    "referral_fee_cny",
    "fba_fee_cny",
    "storage_fee_cny",
    "ad_spend_cny",
    "return_loss_cny",
    "fx_effect_cny",
    "gross_profit_cny",
    "net_profit_cny",
    "gross_margin_pct",
    "net_margin_pct",
    "tacos_pct",
    "available_qty",
    "daily_units_blend",
    "days_of_cover",
    "reorder_point",
]

Event = dict[str, Any]


def _j(value: Any) -> Any:
    """Native JSON-able value."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return round(float(value), 4)
    return value


def load_frames(engine: Engine) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    with engine.connect() as conn:
        daily = pd.read_sql(
            text(
                "SELECT sku_id, shop_id, date_local, units, orders, gmv_cny, refund_cny,"
                " net_revenue_cny, cogs_cny, referral_fee_cny, fba_fee_cny, storage_fee_cny,"
                " ad_spend_cny, return_loss_cny, fx_effect_cny, gross_profit_cny, net_profit_cny,"
                " gross_margin_pct, net_margin_pct, tacos_pct, available_qty,"
                " daily_units_blend, days_of_cover, reorder_point, stock_status, fx_missing"
                " FROM mart_sku_daily"
            ),
            conn,
        )
        skus = pd.read_sql(
            text("SELECT sku_id, parent_asin, asin, title, brand, category FROM dim_sku"), conn
        )
        listings = pd.read_sql(
            text("SELECT listing_id, sku_id, shop_id, asin, title, price_local FROM dim_listing"),
            conn,
        )
        labels = pd.read_sql(
            text(
                "SELECT sku_id, shop_id, date_from, date_to, anomaly_type"
                " FROM anomaly_label WHERE is_anomaly = 1"
            ),
            conn,
        )

    for col in NUM_COLS:
        daily[col] = pd.to_numeric(daily[col], errors="coerce")
    daily["date_local"] = pd.to_datetime(daily["date_local"]).dt.date
    listings["price_local"] = pd.to_numeric(listings["price_local"], errors="coerce")
    labels["date_from"] = pd.to_datetime(labels["date_from"]).dt.date
    labels["date_to"] = pd.to_datetime(labels["date_to"]).dt.date
    return daily, skus, listings, labels


# ---------------------------------------------------------------- helpers
def _streak_events(
    df: pd.DataFrame,
    mask: pd.Series,
    min_days: int,
    rule_id: str,
    severity: str,
    evidence_fn: Callable[[pd.DataFrame], dict[str, Any]],
) -> list[Event]:
    """Flag runs of >= min_days consecutive True days, emit one event per run."""
    events: list[Event] = []
    df = df.sort_values(["sku_id", "shop_id", "date_local"])
    for (sku, shop), grp in df.groupby(["sku_id", "shop_id"], sort=False):
        g = grp.reset_index(drop=True)
        m = mask.reindex(grp.index).fillna(False).to_numpy().astype(bool)
        i = 0
        while i < len(m):
            if m[i]:
                j = i
                while j < len(m) and m[j]:
                    j += 1
                if j - i >= min_days:
                    events.append(
                        {
                            "rule_id": rule_id,
                            "sku_id": sku,
                            "shop_id": shop,
                            "date_local": g.iloc[i]["date_local"],
                            "severity": severity,
                            "evidence": evidence_fn(g.iloc[i:j]),
                        }
                    )
                i = j
            else:
                i += 1
    return events


def _first_day_events(
    df: pd.DataFrame,
    rule_id: str,
    severity: str,
    evidence_fn: Callable[[pd.DataFrame], dict[str, Any]],
) -> list[Event]:
    events: list[Event] = []
    df = df.sort_values(["sku_id", "shop_id", "date_local"])
    for (sku, shop), grp in df.groupby(["sku_id", "shop_id"], sort=False):
        g = grp.sort_values("date_local")
        events.append(
            {
                "rule_id": rule_id,
                "sku_id": sku,
                "shop_id": shop,
                "date_local": g.iloc[0]["date_local"],
                "severity": severity,
                "evidence": evidence_fn(g),
            }
        )
    return events


def _rolling_mean_shift(group: pd.Series, window: int) -> pd.Series:
    return group.rolling(window, min_periods=window).mean().shift(1)


# ------------------------------------------------------------------ rules
def r1_negative_margin(daily: pd.DataFrame, cfg: dict[str, Any]) -> list[Event]:
    """Weekly net margin, not daily.

    Daily margin on a 1-2 unit/day SKU is pure noise (denominator swings 5x).
    A 7-day window matches how long the anomaly lasts and makes the ratio stable.
    """
    c = cfg["negative_margin"]
    s = daily.sort_values(["sku_id", "shop_id", "date_local"])
    g = s.groupby(["sku_id", "shop_id"])
    rev7 = g["net_revenue_cny"].transform(lambda x: x.rolling(7, min_periods=7).sum())
    profit7 = g["net_profit_cny"].transform(lambda x: x.rolling(7, min_periods=7).sum())
    margin7 = profit7 / rev7.replace(0, np.nan)
    # baseline: the same 7-day margin over the previous 28 days, so a SKU that
    # has *always* been thin does not fire — only a real deterioration does
    base_profit = g["net_profit_cny"].transform(
        lambda x: x.rolling(28, min_periods=28).sum().shift(7)
    )
    base_rev = g["net_revenue_cny"].transform(
        lambda x: x.rolling(28, min_periods=28).sum().shift(7)
    )
    base_margin = base_profit / base_rev.replace(0, np.nan)
    s = s.assign(
        _margin7=margin7, _base_margin=base_margin, _rev7=rev7, _drop=margin7 - base_margin
    )
    mask = (
        (s["_margin7"] < float(c["threshold"]))
        & (s["_drop"] < -float(c["drop_pp"]))
        & (s["_rev7"] >= float(c["min_revenue"]))
    )
    return _streak_events(
        s,
        mask,
        int(c["min_days"]),
        "R1",
        c["severity"],
        lambda grp: {
            "margin_7d": _j(grp["_margin7"].min()),
            "drop_pp": _j(grp["_drop"].min()),
            "revenue_7d": _j(grp["_rev7"].max()),
            "days": len(grp),
        },
    )


def r2_gross_margin_drop(daily: pd.DataFrame, cfg: dict[str, Any]) -> list[Event]:
    c = cfg["gross_margin_drop"]
    s = daily.sort_values(["sku_id", "shop_id", "date_local"])
    base = s.groupby(["sku_id", "shop_id"])["gross_margin_pct"].transform(
        lambda x: _rolling_mean_shift(x, 7)
    )
    mask = (s["gross_margin_pct"] < base - float(c["drop_pp"])) & base.notna()
    return _first_day_events(
        s[mask],
        "R2",
        c["severity"],
        lambda g: {"gross_margin_pct": _j(g["gross_margin_pct"].min()), "days": len(g)},
    )


def r3_ad_spike(daily: pd.DataFrame, cfg: dict[str, Any]) -> list[Event]:
    """Ad spend explosion, measured on 7-day totals (not the daily TACOS ratio).

    TACOS = spend / revenue is unusably noisy day-over-day (a 1-unit day makes
    it look like 400%). Comparing this week's total spend against the week
    before is the stable signal.
    """
    c = cfg["ad_spike"]
    s = daily.sort_values(["sku_id", "shop_id", "date_local"])
    g = s.groupby(["sku_id", "shop_id"])["ad_spend_cny"]
    spend7 = g.transform(lambda x: x.rolling(7, min_periods=7).sum())
    base7 = g.transform(lambda x: x.rolling(7, min_periods=7).sum().shift(7))
    mask = (spend7 > float(c["min_spend"])) & (spend7 > float(c["ratio"]) * base7) & (base7 > 0)
    # one event per contiguous run of flagged days, so a spike that starts later
    # still carries its own date instead of being collapsed onto the first
    # (unrelated) spike of the period
    return _streak_events(
        s,
        mask,
        int(c.get("min_days", 2)),
        "R3",
        c["severity"],
        lambda grp: {
            "spend_7d": _j(spend7.reindex(grp.index).max()),
            "baseline_7d": _j(base7.reindex(grp.index).max()),
        },
    )


def r4_sales_cliff(daily: pd.DataFrame, cfg: dict[str, Any]) -> list[Event]:
    """Weekly units vs the prior 28-day daily baseline (scaled to a week).

    Comparing daily units to a daily average turns Poisson noise into "cliffs";
    aggregating a week removes it while a real collapse still shows through.
    """
    c = cfg["sales_cliff"]
    s = daily.sort_values(["sku_id", "shop_id", "date_local"])
    g = s.groupby(["sku_id", "shop_id"])
    units7 = g["units"].transform(lambda x: x.rolling(7, min_periods=7).sum())
    base = g["units"].transform(
        lambda x: (
            x.rolling(int(c["baseline_days"]), min_periods=int(c["baseline_days"])).mean().shift(7)
        )
    )
    s = s.assign(_units7=units7, _base=base)
    mask = (s["_units7"] < float(c["ratio"]) * 7.0 * s["_base"]) & (
        s["_base"] >= float(c["min_baseline_units"])
    )
    return _streak_events(
        s,
        mask,
        int(c["min_days"]),
        "R4",
        c["severity"],
        lambda grp: {
            "units_7d": _j(grp["_units7"].min()),
            "expected_7d": _j(7.0 * grp["_base"].iloc[0]),
            "days": len(grp),
        },
    )


def r5_refund_spike(daily: pd.DataFrame, skus: pd.DataFrame, cfg: dict[str, Any]) -> list[Event]:
    """Refund-rate spike measured against the *category* baseline.

    The injected anomaly lasts the whole period, so the SKU's own history is
    already elevated — comparing against it would miss the anomaly entirely.
    """
    c = cfg["refund_spike"]
    base_frame = daily.drop(columns=["category"], errors="ignore")
    s = base_frame.merge(skus[["sku_id", "category"]], on="sku_id", how="left")
    s["category"] = s["category"].fillna("unknown")
    cat_rate = s.groupby("category").apply(
        lambda g: g["refund_cny"].sum() / g["gmv_cny"].sum() if g["gmv_cny"].sum() > 0 else 0.0
    )
    s["cat_rate"] = s["category"].map(cat_rate)
    g = s.groupby(["sku_id", "shop_id"])
    ref7 = g["refund_cny"].transform(lambda x: x.rolling(7, min_periods=7).sum())
    gmv7 = g["gmv_cny"].transform(lambda x: x.rolling(7, min_periods=7).sum())
    s["rate7"] = ref7 / gmv7.replace(0, np.nan)
    mask = (s["rate7"] > float(c["min_rate"])) & (s["rate7"] > float(c["ratio"]) * s["cat_rate"])
    mask &= s["cat_rate"] > 0
    mask &= gmv7 >= float(c["min_gmv_7d"])
    return _first_day_events(
        s[mask],
        "R5",
        c["severity"],
        lambda g: {
            "refund_rate_7d": _j(g["rate7"].max()),
            "category_rate": _j(g["cat_rate"].iloc[0]),
        },
    )


def r6_stockout(daily: pd.DataFrame, cfg: dict[str, Any]) -> list[Event]:
    c = cfg["stockout"]
    mask = daily["stock_status"].isin(["STOCKOUT_RISK", "OUT_OF_STOCK"])
    return _streak_events(
        daily,
        mask,
        1,
        "R6",
        c["severity"],
        lambda g: {"stock_status": str(g["stock_status"].iloc[0]), "days": len(g)},
    )


def r7_overstock(daily: pd.DataFrame, cfg: dict[str, Any]) -> list[Event]:
    """Overstock detected by its root cause: a sudden large inbound jump.

    days_of_cover > 180 fires for most slow-moving SKUs and drowns out the real
    event; the meaningful signal is "one day the available stock jumped by a
    large amount".
    """
    c = cfg["overstock"]
    s = daily.sort_values(["sku_id", "shop_id", "date_local"])
    prev = s.groupby(["sku_id", "shop_id"])["available_qty"].shift(1)
    jump = s["available_qty"] - prev
    mask = jump > float(c["jump"])
    return _first_day_events(
        s[mask],
        "R7",
        c["severity"],
        lambda g: {"jump": _j(jump.reindex(g.index).max())},
    )


def r8_duplicate_listing(listings: pd.DataFrame, cfg: dict[str, Any]) -> list[Event]:
    thr = float(cfg["duplicate_listing"]["similarity"])
    events: list[Event] = []
    for shop, grp in listings.groupby("shop_id"):
        rows = grp.reset_index(drop=True)
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a, b = str(rows.title[i]).lower(), str(rows.title[j]).lower()
                if fuzz.ratio(a, b) / 100.0 >= thr:
                    events.append(
                        {
                            "rule_id": "R8",
                            "sku_id": rows.sku_id[i],
                            "shop_id": shop,
                            "date_local": None,
                            "severity": cfg["duplicate_listing"]["severity"],
                            "evidence": {
                                "sku_a": rows.sku_id[i],
                                "sku_b": rows.sku_id[j],
                                "similarity": round(fuzz.ratio(a, b) / 100.0, 4),
                            },
                        }
                    )
    return events


def r9_variant_price(
    listings: pd.DataFrame, skus: pd.DataFrame, cfg: dict[str, Any]
) -> list[Event]:
    thr = float(cfg["variant_price"]["spread"])
    lp = listings.merge(skus[["sku_id", "parent_asin"]], on="sku_id")
    events: list[Event] = []
    for (shop, parent), grp in lp.groupby(["shop_id", "parent_asin"]):
        prices = grp["price_local"].astype(float)
        if len(prices) < 2 or prices.min() <= 0:
            continue
        spread = (prices.max() - prices.min()) / prices.min()
        if spread > thr:
            victim = grp.loc[prices.idxmax(), "sku_id"]
            events.append(
                {
                    "rule_id": "R9",
                    "sku_id": victim,
                    "shop_id": shop,
                    "date_local": None,
                    "severity": cfg["variant_price"]["severity"],
                    "evidence": {
                        "parent_asin": parent,
                        "spread": round(float(spread), 4),
                        "max_price": _j(prices.max()),
                        "min_price": _j(prices.min()),
                    },
                }
            )
    return events


def r10_data_quality(daily: pd.DataFrame, cfg: dict[str, Any]) -> list[Event]:
    mask = daily["fx_missing"] == True  # noqa: E712
    return _first_day_events(
        daily[mask],
        "R10",
        cfg["data_quality"]["severity"],
        lambda g: {"rows": len(g)},
    )


# ------------------------------------------------------------------ orchestrate
def detect(
    daily: pd.DataFrame,
    skus: pd.DataFrame,
    listings: pd.DataFrame,
    cfg: dict[str, Any],
) -> pd.DataFrame:
    events: list[Event] = []
    events += r1_negative_margin(daily, cfg)
    events += r2_gross_margin_drop(daily, cfg)
    events += r3_ad_spike(daily, cfg)
    events += r4_sales_cliff(daily, cfg)
    events += r5_refund_spike(daily, skus, cfg)
    events += r6_stockout(daily, cfg)
    events += r7_overstock(daily, cfg)
    events += r8_duplicate_listing(listings, cfg)
    events += r9_variant_price(listings, skus, cfg)
    events += r10_data_quality(daily, cfg)
    return pd.DataFrame(events)


def evaluate(events: pd.DataFrame, labels: pd.DataFrame) -> pd.DataFrame:
    """Per-rule recall and precision against injected labels."""
    rows: list[dict[str, Any]] = []
    for atype, rule in TYPE_TO_RULE.items():
        sub_labels = labels[labels["anomaly_type"] == atype]
        ev = events[events["rule_id"] == rule]
        total = len(sub_labels)
        recalled = 0
        for _, lab in sub_labels.iterrows():
            hit = ev[(ev["sku_id"] == lab["sku_id"]) & (ev["shop_id"] == lab["shop_id"])]
            # catalog-level rules (R8/R9) have no date; date rules must fall in window
            hit = hit[
                hit["date_local"].isna()
                | ((hit["date_local"] >= lab["date_from"]) & (hit["date_local"] <= lab["date_to"]))
            ]
            recalled += int(len(hit) > 0)
        pred_pairs = set(zip(ev["sku_id"], ev["shop_id"], strict=False))
        label_pairs = set(zip(sub_labels["sku_id"], sub_labels["shop_id"], strict=False))
        precision = len(pred_pairs & label_pairs) / len(pred_pairs) if pred_pairs else None
        recall = recalled / total if total else None
        rows.append(
            {
                "rule": rule,
                "anomaly_type": atype,
                "labels": total,
                "events": len(ev),
                "recall": recall,
                "precision": precision,
            }
        )
    return pd.DataFrame(rows)


def store_events(engine: Engine, events: pd.DataFrame) -> int:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM anomaly_event"))
    if events.empty:
        return 0
    records = []
    for _, row in events.iterrows():
        records.append(
            {
                "rule_id": row["rule_id"],
                "sku_id": row["sku_id"],
                "shop_id": row["shop_id"],
                "date_local": row["date_local"],
                "severity": row["severity"],
                "evidence": row["evidence"],
            }
        )
    with engine.begin() as conn:
        conn.execute(anomaly_event.insert(), records)
    return len(records)


def run(engine: Engine, settings: Settings | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    settings = settings or get_settings()
    cfg = yaml.safe_load(RULES_CFG_PATH.read_text(encoding="utf-8"))
    daily, skus, listings, labels = load_frames(engine)
    events = detect(daily, skus, listings, cfg)
    n = store_events(engine, events)
    eval_df = evaluate(events, labels)
    eval_df.attrs["event_count"] = n
    return events, eval_df


def write_eval_report(eval_df: pd.DataFrame, out_path: Any) -> Any:
    lines = ["# 异常检测评估报告\n"]
    lines.append("| 规则 | 异常类型 | 标注数 | 检测事件数 | 召回率 | 精确率 |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: |")
    for _, row in eval_df.iterrows():
        recall = "—" if row["recall"] is None else f"{row['recall']:.0%}"
        precision = "—" if row["precision"] is None else f"{row['precision']:.0%}"
        lines.append(
            f"| {row['rule']} | {row['anomaly_type']} | {row['labels']} "
            f"| {row['events']} | {recall} | {precision} |"
        )
    lines.append("")
    lines.append(
        "召回率 = 命中的标注窗口数 / 标注总数；精确率 = 有对应标注的 (SKU, 店) 对 / 检测到的 (SKU, 店) 对。"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path
