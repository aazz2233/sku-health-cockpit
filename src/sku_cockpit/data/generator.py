"""Synthetic marketplace generator.

Design goals (see docs/design-p1-data-layer.md):
  * reproducible   - fixed seed, sorted iteration, no wall-clock input
  * realistic      - long tail, weekly cycle, promo pulses, refund lag
  * *labelled*     - injected anomalies are recorded as ground truth
  * *dirty*        - 8 classes of bad rows are recorded in a manifest so the
                     validation layer can be tested for recall, not just "runs"
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from sku_cockpit.config import ROOT
from sku_cockpit.data.constants import (
    BRANDS,
    CATEGORIES,
    CATEGORY_BY_CODE,
    COLORS,
    DAYS,
    FULFILLMENT_METHODS,
    PRODUCTS,
    PROMO_WINDOWS,
    REFUND_REASONS,
    SHOPS,
    START_DATE,
    WAREHOUSES,
    Category,
    Shop,
)

# UTC offsets used while the generator is running (summer time).
UTC_OFFSET: dict[str, int] = {"US": -7, "DE": 2, "UK": 1}

DIRTY_TYPES = {
    "D1": ("duplicate row", "STRUCT_DUP_KEY"),
    "D2": ("missing price", "STRUCT_NULL"),
    "D3": ("unknown sku", "BIZ_FK_MISSING"),
    "D4": ("currency mismatch", "BIZ_CURRENCY"),
    "D5": ("negative quantity", "STRUCT_RANGE"),
    "D6": ("invalid date", "STRUCT_TYPE"),
    "D7": ("dirty header", "MAPPING"),
    "D8": ("sku case mismatch", "BIZ_FK_MISSING"),
}


@dataclass
class FileMeta:
    file: str
    shop_id: str
    dataset: str
    rows: int
    header_row: int
    dirty: list[dict[str, object]] = field(default_factory=list)


@dataclass
class GenerationResult:
    seed: int
    output_dir: Path
    files: list[FileMeta]
    labels: pd.DataFrame
    manifest_path: Path


class Generator:
    def __init__(
        self,
        seed: int = 20241119,
        n_skus: int = 200,
        days: int = DAYS,
        start: date = START_DATE,
        shops: tuple[Shop, ...] = SHOPS,
        out_dir: Path | None = None,
    ) -> None:
        self.seed = seed
        self.rng = np.random.default_rng(seed)
        self.n_skus = n_skus
        self.days = days
        self.start = start
        self.shops = shops
        self.out_dir = out_dir
        self.dates = [start + timedelta(days=i) for i in range(days)]
        self.promo_day = self._promo_map()

        # anomaly plan (filled by _plan_anomalies)
        self.price_factor: dict[str, tuple[int, int, float]] = {}
        self.ad_factor: dict[str, tuple[int, int, float]] = {}
        self.sales_cliff: dict[str, tuple[int, float]] = {}
        self.refund_factor: dict[str, float] = {}
        self.low_stock: dict[str, float] = {}
        self.overstock: dict[str, tuple[int, float]] = {}
        self.labels: list[dict[str, object]] = []

        self.shops_df = self._build_shops()
        self.skus, self.listings = self._build_catalog()
        self.fee_schedule = self._build_fee_schedule()
        self.fx = self._build_fx()
        self.calendar = self._build_calendar()
        self.shipments = self._build_shipments()
        self.logistics = self._build_logistics()
        self._plan_anomalies()
        self._apply_catalog_anomalies()

    # ------------------------------------------------------------------ dims
    def _promo_map(self) -> dict[int, str]:
        promo: dict[int, str] = {}
        for start, end, name in PROMO_WINDOWS:
            for d in range(start, min(end, self.days)):
                promo[d] = name
        return promo

    def _build_shops(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "shop_id": s.shop_id,
                    "shop_name": s.shop_name,
                    "marketplace": s.marketplace,
                    "currency": s.currency,
                    "timezone": s.timezone,
                    "status": "active",
                }
                for s in self.shops
            ]
        )

    def _build_catalog(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        rng = self.rng
        rows: list[dict[str, object]] = []
        listings: list[dict[str, object]] = []

        # 30 parent products, 2..5 variants each, so 200 SKUs roughly
        n_parents = max(1, self.n_skus // 6)
        parents: list[str] = [f"B0{rng.integers(10_000_000, 99_999_999)}" for _ in range(n_parents)]
        per_parent: dict[str, int] = {}
        for i in range(self.n_skus):
            per_parent[parents[i % n_parents]] = per_parent.get(parents[i % n_parents], 0) + 1

        for i in range(self.n_skus):
            cat: Category = CATEGORIES[i % len(CATEGORIES)]
            parent = parents[i % n_parents]
            brand = BRANDS[i % len(BRANDS)]
            product = PRODUCTS[cat.code][i % len(PRODUCTS[cat.code])]
            color = COLORS[i % len(COLORS)]
            # model code keeps titles unique: without it, 6 categories x 4
            # products x 6 colours produced many identical titles, which made
            # the duplicate-listing rule flag half the catalogue
            title = f"{brand} {product} - {color} M{i + 1:03d}"
            price_usd = float(cat.price_mid_usd * math.exp(rng.normal(0.0, 0.25)))
            price_usd = round(max(4.99, price_usd), 2)
            cost_cny = round(
                price_usd * self.shops[0].fx_rate * (1 - cat.margin_target) * rng.uniform(0.9, 1.1),
                2,
            )
            launch = self.start - timedelta(days=int(rng.integers(30, 900)))
            lifecycle = (
                "new" if rng.random() < 0.10 else ("phaseout" if rng.random() < 0.05 else "active")
            )
            sku_id = f"SKU-{i + 1:04d}"
            asin = f"{parent[:2]}{rng.integers(10**7, 10**8 - 1)}"
            # unit volume for storage fees; deterministic jitter so the RNG
            # stream (and therefore every other dataset) stays unchanged
            jitter = 0.85 + 0.3 * ((i * 37) % 100) / 100.0
            unit_cbm = round(cat.weight_kg / 200.0 * jitter, 6)
            rows.append(
                {
                    "sku_id": sku_id,
                    "parent_asin": parent,
                    "asin": asin,
                    "title": title,
                    "brand": brand,
                    "category": cat.code,
                    "product_type": product,
                    "unit_cost_cny": cost_cny,
                    "unit_cbm": unit_cbm,
                    "launch_date": launch,
                    "lifecycle": lifecycle,
                    "_price_usd": price_usd,
                }
            )
            for shop in self.shops:
                # price_usd is a USD list price; convert through CNY so each
                # marketplace keeps a sensible local price (no 7x USD bug).
                price_local = round(price_usd * self.shops[0].fx_rate / shop.fx_rate, 2)
                listings.append(
                    {
                        "listing_id": f"{shop.shop_id}-B0{i + 1:06d}",
                        "sku_id": sku_id,
                        "shop_id": shop.shop_id,
                        "asin": f"{parent[:2]}{rng.integers(10**7, 10**8 - 1)}",
                        "title": f"{title} ({shop.marketplace})",
                        "price_local": price_local,
                        "currency": shop.currency,
                        "status": "active",
                    }
                )

        skus = pd.DataFrame(rows)
        lst = pd.DataFrame(listings)

        # A7: duplicate listings (same brand/model, near-identical title)
        dup_idx = rng.choice(len(skus), size=3, replace=False)
        for n, idx in enumerate(sorted(dup_idx.tolist())):
            base = skus.iloc[int(idx)]
            shop = self.shops[n % len(self.shops)]
            lst.loc[len(lst)] = {
                "listing_id": f"{shop.shop_id}-DUP{n + 1:03d}",
                "sku_id": base["sku_id"],
                "shop_id": shop.shop_id,
                "asin": f"{base['parent_asin'][:2]}{rng.integers(10**7, 10**8 - 1)}",
                "title": f"{base['title']} {shop.marketplace}",  # almost the same title
                "price_local": round(
                    float(base["_price_usd"]) * self.shops[0].fx_rate / shop.fx_rate, 2
                ),
                "currency": shop.currency,
                "status": "active",
            }
        return skus, lst

    def _build_fee_schedule(self) -> pd.DataFrame:
        """Platform fees are quoted in USD-equivalents, then converted to local.

        Bug fixed here: multiplying a USD fee by the local->CNY rate produced
        fees ~7x too high (a $3.20 FBA fee became "22.88 USD" for the US shop).
        """
        rows = []
        usd_fx = self.shops[0].fx_rate
        for shop in self.shops:
            local_per_usd = usd_fx / shop.fx_rate
            for cat in CATEGORIES:
                rows.append(
                    {
                        "category": cat.code,
                        "marketplace": shop.marketplace,
                        "referral_rate": cat.referral_rate,
                        "referral_min": round(0.30 * local_per_usd, 2),
                        "fba_size_tier": "standard" if cat.weight_kg < 1.0 else "large",
                        "fba_weight_tier": "lt_1kg" if cat.weight_kg <= 1.0 else "1_3kg",
                        "fba_fee_local": round(cat.fba_fee_usd * local_per_usd, 2),
                        # 23 USD per cubic metre per month, in local currency
                        "storage_rate_per_cbm_month": round(23.0 * local_per_usd, 2),
                        "effective_from": self.start - timedelta(days=365),
                        "effective_to": None,
                    }
                )
        return pd.DataFrame(rows)

    def _build_fx(self) -> pd.DataFrame:
        rows = []
        for shop in self.shops:
            value = shop.fx_rate
            for d in self.dates:
                value *= float(math.exp(self.rng.normal(0.0, 0.004)))
                rows.append(
                    {
                        "date": d,
                        "currency": shop.currency,
                        "rate_to_cny": round(value, 4),
                    }
                )
        return pd.DataFrame(rows)

    def _build_calendar(self) -> pd.DataFrame:
        rows = []
        for d in self.dates:
            rows.append(
                {
                    "date": d,
                    "year": d.year,
                    "month": d.month,
                    "week": int(d.strftime("%W")),
                    "weekday": d.weekday(),
                    "is_promo": d.weekday() >= 0
                    and self.promo_day.get((d - self.start).days) is not None,
                    "promo_name": self.promo_day.get((d - self.start).days),
                }
            )
        return pd.DataFrame(rows)

    def _build_shipments(self) -> pd.DataFrame:
        rows = []
        for i in range(len(self.skus)):
            sku = f"SKU-{i + 1:04d}"
            n_batches = int(self.rng.integers(2, 5))
            for b in range(n_batches):
                arrived = self.start + timedelta(days=int(self.rng.integers(-20, self.days)))
                qty = int(self.rng.integers(60, 400))
                # inbound freight: 0.5-1.6 USD per unit (3-11 CNY), a realistic
                # share of a 20-60 USD product instead of a 25% margin killer
                rows.append(
                    {
                        "shipment_id": f"SHP-{i + 1:04d}-{b + 1}",
                        "sku_id": sku,
                        "qty": qty,
                        "freight_cny": round(qty * self.rng.uniform(0.5, 1.6) * 7.15, 2),
                        "arrived_at": arrived,
                        "warehouse": WAREHOUSES[b % len(WAREHOUSES)],
                    }
                )
        return pd.DataFrame(rows)

    def _build_logistics(self) -> dict[str, dict[str, float]]:
        """Per-sku demand intensity and ad intensity."""
        out: dict[str, dict[str, float]] = {}
        for i in range(len(self.skus)):
            sku = f"SKU-{i + 1:04d}"
            base = float(math.exp(self.rng.normal(0.0, 0.7)))  # long tail
            out[sku] = {
                "lambda": round(min(6.0, 0.55 + base), 3),
                "tacos": float(np.clip(self.rng.normal(0.12, 0.05), 0.03, 0.35)),
                "ad_prob": float(np.clip(self.rng.normal(0.65, 0.15), 0.2, 0.95)),
            }
        return out

    # -------------------------------------------------------------- anomalies
    def _window(self, start_fraction: float, length: int) -> tuple[int, int]:
        """Anomaly windows are proportional to the period, so `days=20` works too."""
        start = min(max(0, int(self.days * start_fraction)), self.days - 1)
        return start, min(self.days - 1, start + length)

    def _plan_anomalies(self) -> None:
        rng = self.rng
        n = len(self.skus)
        picks = rng.permutation(n)
        cursor = 0

        def take(k: int) -> list[str]:
            nonlocal cursor
            ids = [f"SKU-{int(i) + 1:04d}" for i in picks[cursor : cursor + k]]
            cursor += k
            return ids

        # A1 continuous negative margin: price drop 25% for two weeks
        d0, d1 = self._window(0.45, 14)
        for sku in take(5):
            self.price_factor[sku] = (d0, d1, 0.75)
            self.labels.extend(self._label(sku, d0, d1, "negative_margin"))
        # A2 ad spend explosion for one week
        d0, d1 = self._window(0.33, 7)
        for sku in take(3):
            self.ad_factor[sku] = (d0, d1, 3.0)
            self.labels.extend(self._label(sku, d0, d1, "ad_spike"))
        # A3 sales cliff from the middle onwards
        d0, d1 = self._window(0.5, self.days)
        for sku in take(4):
            self.sales_cliff[sku] = (d0, 0.30)
            self.labels.extend(self._label(sku, d0, d1, "sales_cliff"))
        # A4 refund rate explosion
        for sku in take(3):
            self.refund_factor[sku] = 6.0
            self.labels.extend(self._label(sku, 0, self.days - 1, "refund_spike"))
        # A5 stockout risk in the last stretch
        d0 = max(0, self.days - 15)
        for sku in take(6):
            self.low_stock[sku] = 0.06
            self.labels.extend(self._label(sku, d0, self.days - 1, "stockout_risk"))
        # A6 overstock: big inbound then demand collapses
        d0, d1 = self._window(0.55, self.days)
        for sku in take(4):
            self.overstock[sku] = (d0, 0.05)
            self.labels.extend(self._label(sku, d0, d1, "overstock"))

    def _label(self, sku: str, d0: int, d1: int, kind: str) -> list[dict[str, object]]:
        """An anomaly hits the SKU in every shop it is listed in."""
        return [
            {
                "sku_id": sku,
                "shop_id": shop.shop_id,
                "date_from": self.dates[max(0, d0)],
                "date_to": self.dates[min(self.days - 1, d1)],
                "anomaly_type": kind,
                "is_anomaly": True,
                "source": "injected",
            }
            for shop in self.shops
        ]

    def _apply_catalog_anomalies(self) -> None:
        """A8: one variant priced far above its siblings, in every shop."""
        by_parent = self.skus.groupby("parent_asin")["sku_id"].apply(list)
        candidates = [(p, ids) for p, ids in by_parent.items() if len(ids) >= 3]
        if not candidates:
            return
        for _parent, ids in candidates[:2]:
            victim = sorted(ids)[0]
            # make the victim clearly the most expensive variant, so the rule
            # (which flags the highest-priced child) points at exactly this SKU
            group = self.listings[self.listings["sku_id"].isin(ids)]
            max_price = float(group["price_local"].astype(float).max())
            mask = self.listings["sku_id"] == victim
            self.listings.loc[mask, "price_local"] = round(max_price * 1.4, 2)
            self.labels.extend(self._label(victim, 0, self.days - 1, "variant_price"))

    # -------------------------------------------------------------- simulate
    def _price_factor(self, sku: str, day: int) -> float:
        if sku in self.price_factor:
            d0, d1, f = self.price_factor[sku]
            if d0 <= day <= d1:
                return f
        return 1.0

    def _sales_factor(self, sku: str, day: int) -> float:
        f = 1.0
        if sku in self.sales_cliff:
            start, mult = self.sales_cliff[sku]
            if day >= start:
                f *= mult
        if sku in self.overstock:
            start, mult = self.overstock[sku]
            if day >= start:
                f *= mult
        return f

    def run(self) -> GenerationResult:
        orders, refunds, ads, inventory, settlement = self._simulate()
        labels = pd.DataFrame(self.labels) if self.labels else pd.DataFrame(columns=["sku_id"])
        return self._write(orders, refunds, ads, inventory, settlement, labels)

    def _simulate(
        self,
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        rng = self.rng
        order_rows: list[dict[str, object]] = []
        refund_rows: list[dict[str, object]] = []
        ad_rows: list[dict[str, object]] = []
        inv_rows: list[dict[str, object]] = []
        settle_rows: list[dict[str, object]] = []

        sku_ids = sorted(self.skus["sku_id"].tolist())
        cat_of = dict(zip(self.skus["sku_id"], self.skus["category"], strict=True))
        price_usd_of = dict(zip(self.skus["sku_id"], self.skus["_price_usd"], strict=True))
        fee_of_cat = {
            (str(r["category"]), str(r["marketplace"])): (
                float(r["referral_rate"]),
                float(r["fba_fee_local"]),
            )
            for _, r in self.fee_schedule.iterrows()
        }
        inbound_by_sku_day: dict[tuple[str, int], int] = {}
        for _, s in self.shipments.iterrows():
            day_idx = (s["arrived_at"] - self.start).days
            if 0 <= day_idx < self.days:
                key = (str(s["sku_id"]), int(day_idx))
                inbound_by_sku_day[key] = inbound_by_sku_day.get(key, 0) + int(s["qty"])

        listing_of: dict[tuple[str, str], str] = {}
        for _, row in self.listings.iterrows():
            listing_key = (str(row["shop_id"]), str(row["sku_id"]))
            if listing_key not in listing_of:
                listing_of[listing_key] = str(row["listing_id"])

        for shop in self.shops:
            offset = UTC_OFFSET[shop.marketplace]
            for sku in sku_ids:
                cat = CATEGORY_BY_CODE[cat_of[sku]]
                base_lambda = self.logistics[sku]["lambda"]
                tacos = self.logistics[sku]["tacos"]
                ad_prob = self.logistics[sku]["ad_prob"]
                stock = 400.0
                for day_idx, d in enumerate(self.dates):
                    weekday = 1.25 if d.weekday() >= 5 else 1.0
                    promo = 2.2 if day_idx in self.promo_day else 1.0
                    lifecycle = 1.0
                    lam = base_lambda * weekday * promo * lifecycle
                    lam *= self._sales_factor(sku, day_idx)
                    lam = max(0.0, lam)
                    units = int(rng.poisson(lam)) if lam > 0 else 0

                    # inventory
                    inbound = inbound_by_sku_day.get((sku, day_idx), 0)
                    available = max(0.0, stock + inbound - units)
                    stock = available
                    if sku in self.low_stock and day_idx >= self.days - 15:
                        available = max(0.0, available * float(self.low_stock[sku]))
                    if sku in self.overstock and day_idx == self.overstock[sku][0]:
                        available += 2500
                    inv_rows.append(
                        {
                            "snapshot_id": f"{shop.shop_id}-{sku}-{d.isoformat()}",
                            "shop_id": shop.shop_id,
                            "sku_id": sku,
                            "warehouse": "FBA",
                            "date_local": d,
                            "available_qty": int(available),
                            "inbound_qty": int(inbound),
                            "reserved_qty": int(min(units, available) // 3),
                        }
                    )

                    if units == 0:
                        continue

                    price_local = round(
                        float(price_usd_of[sku])
                        * self.shops[0].fx_rate
                        / shop.fx_rate
                        * self._price_factor(sku, day_idx),
                        2,
                    )
                    remaining = units
                    line_no = 0
                    while remaining > 0:
                        line_no += 1
                        qty = 1 if remaining == 1 else int(rng.integers(1, min(3, remaining) + 1))
                        remaining -= qty
                        # 15% of order lines merge a second SKU (same order, same day)
                        hour = int(rng.integers(6, 22))
                        minute = int(rng.integers(0, 60))
                        local_dt = datetime(d.year, d.month, d.day, hour, minute)
                        utc_dt = local_dt - timedelta(hours=offset)
                        order_id = (
                            f"{int(rng.integers(100, 999))}-{int(rng.integers(1000000, 9999999))}-"
                            f"{int(rng.integers(1000000, 9999999))}"
                        )
                        item_id = f"{order_id}-{line_no}"
                        amount = round(qty * price_local, 2)
                        fulfillment = FULFILLMENT_METHODS[
                            int(rng.integers(0, len(FULFILLMENT_METHODS)))
                        ]
                        order_rows.append(
                            {
                                "order_item_id": item_id,
                                "order_id": order_id,
                                "shop_id": shop.shop_id,
                                "sku_id": sku,
                                "listing_id": listing_of[(shop.shop_id, sku)],
                                "qty": qty,
                                "unit_price_local": price_local,
                                "item_total_local": amount,
                                "order_total_local": amount,
                                "currency": shop.currency,
                                "order_at_utc": utc_dt.replace(tzinfo=UTC),
                                "order_time_local": local_dt.strftime("%Y-%m-%d %H:%M:%S"),
                                "order_date_local": d,
                                "fulfillment": fulfillment,
                                "status": "Shipped",
                            }
                        )

                        # settlement: referral + fba
                        rate, fba_fee = fee_of_cat[(cat.code, shop.marketplace)]
                        referral = -round(max(amount * rate, 0.30), 2)
                        settle_rows.append(
                            {
                                "settlement_id": f"{item_id}-REF",
                                "shop_id": shop.shop_id,
                                "order_item_id": item_id,
                                "date_local": d,
                                "fee_type": "referral",
                                "amount_local": referral,
                                "currency": shop.currency,
                            }
                        )
                        settle_rows.append(
                            {
                                "settlement_id": f"{item_id}-FBA",
                                "shop_id": shop.shop_id,
                                "order_item_id": item_id,
                                "date_local": d,
                                "fee_type": "fba_fulfillment",
                                "amount_local": -round(fba_fee, 2),
                                "currency": shop.currency,
                            }
                        )

                        # refunds
                        refund_rate = cat.refund_rate * self.refund_factor.get(sku, 1.0)
                        if rng.random() < min(0.6, refund_rate):
                            lag = int(rng.integers(3, 21))
                            r_date = d + timedelta(days=lag)
                            reason = REFUND_REASONS[int(rng.integers(0, len(REFUND_REASONS)))]
                            refund_rows.append(
                                {
                                    "refund_id": f"REF-{item_id}",
                                    "order_item_id": item_id,
                                    "shop_id": shop.shop_id,
                                    "sku_id": sku,
                                    "qty": qty,
                                    "amount_local": round(amount * 0.95, 2),
                                    "currency": shop.currency,
                                    "reason_code": reason,
                                    "refund_at_utc": datetime(
                                        r_date.year, r_date.month, r_date.day, 12, 0
                                    ),
                                    "refund_date_local": r_date,
                                    "restockable": bool(
                                        reason in ("buyer_remorse", "size_mismatch")
                                    ),
                                }
                            )

                        # ads (per sku-day, attach to the first line only)
                        if line_no == 1 and rng.random() < ad_prob:
                            spend = round(amount * tacos, 2)
                            if sku in self.ad_factor:
                                d0, d1, factor = self.ad_factor[sku]
                                if d0 <= day_idx <= d1:
                                    spend = round(spend * factor, 2)
                            ad_rows.append(
                                {
                                    "ad_spend_id": f"{shop.shop_id}-{sku}-{d.isoformat()}",
                                    "shop_id": shop.shop_id,
                                    "sku_id": sku,
                                    "campaign_id": f"CMP-{shop.shop_id}-{sku[-3:]}",
                                    "campaign_type": "SP",
                                    "date_local": d,
                                    "impressions": int(
                                        max(100, units * int(rng.integers(120, 260)))
                                    ),
                                    "clicks": int(max(1, units * int(rng.integers(6, 18)))),
                                    "spend_local": spend,
                                    "attributed_sales_local": amount,
                                    "currency": shop.currency,
                                }
                            )

        return (
            pd.DataFrame(order_rows),
            pd.DataFrame(refund_rows),
            pd.DataFrame(ad_rows),
            pd.DataFrame(inv_rows),
            pd.DataFrame(settle_rows),
        )

    # ----------------------------------------------------------------- write
    def _write(
        self,
        orders: pd.DataFrame,
        refunds: pd.DataFrame,
        ads: pd.DataFrame,
        inventory: pd.DataFrame,
        settlement: pd.DataFrame,
        labels: pd.DataFrame,
    ) -> GenerationResult:
        out = self.out_dir or (ROOT / "data" / "generated" / f"seed{self.seed}")
        out.mkdir(parents=True, exist_ok=True)
        files: list[FileMeta] = []

        self.shops_df.to_csv(out / "shops.csv", index=False, encoding="utf-8-sig")
        self.skus.drop(columns=["_price_usd"]).to_csv(
            out / "skus.csv", index=False, encoding="utf-8-sig"
        )
        self.listings.to_csv(out / "listings.csv", index=False, encoding="utf-8-sig")
        self.shipments.to_csv(out / "shipments.csv", index=False, encoding="utf-8-sig")
        self.fee_schedule.to_csv(out / "fee_schedule.csv", index=False, encoding="utf-8-sig")
        self.fx.to_csv(out / "fx_rates.csv", index=False, encoding="utf-8-sig")
        self.calendar.to_csv(out / "dim_date.csv", index=False, encoding="utf-8-sig")
        labels.to_csv(out / "anomaly_labels.csv", index=False, encoding="utf-8-sig")

        for shop in self.shops:
            sid = shop.shop_id
            files.extend(
                self._write_shops_files(out, sid, orders, refunds, ads, inventory, settlement)
            )

        manifest = {
            "seed": self.seed,
            "start_date": self.start.isoformat(),
            "days": self.days,
            "files": [
                {
                    "file": f.file,
                    "shop_id": f.shop_id,
                    "dataset": f.dataset,
                    "rows": f.rows,
                    "header_row": f.header_row,
                    "dirty": f.dirty,
                }
                for f in files
            ],
        }
        manifest_path = out / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return GenerationResult(
            seed=self.seed, output_dir=out, files=files, labels=labels, manifest_path=manifest_path
        )

    def _write_shops_files(
        self,
        out: Path,
        shop_id: str,
        orders: pd.DataFrame,
        refunds: pd.DataFrame,
        ads: pd.DataFrame,
        inventory: pd.DataFrame,
        settlement: pd.DataFrame,
    ) -> list[FileMeta]:
        """Write one shop's exports using *source-style* headers.

        Each shop exports different column names on purpose: that is exactly
        what the YAML mapping layer exists for.
        """
        meta: list[FileMeta] = []

        o = orders[orders["shop_id"] == shop_id].copy()
        r = refunds[refunds["shop_id"] == shop_id].copy()
        a = ads[ads["shop_id"] == shop_id].copy()
        i = inventory[inventory["shop_id"] == shop_id].copy()
        s = settlement[settlement["shop_id"] == shop_id].copy()

        exports: dict[str, pd.DataFrame] = {
            "orders": pd.DataFrame(
                {
                    "Order Item ID": o["order_item_id"].astype(str),
                    "Order ID": o["order_id"].astype(str),
                    "Purchase Date": o["order_time_local"].astype(str),
                    "Seller SKU": o["sku_id"].astype(str),
                    "Quantity": o["qty"].astype(int),
                    "Item Price": o["unit_price_local"].astype(float),
                    "Currency": o["currency"].astype(str),
                    "Fulfillment": o["fulfillment"].astype(str),
                    "Status": o["status"].astype(str),
                }
            ).reset_index(drop=True),
            "refunds": pd.DataFrame(
                {
                    "Refund ID": r["refund_id"].astype(str),
                    "Order Item ID": r["order_item_id"].astype(str),
                    "Seller SKU": r["sku_id"].astype(str),
                    "Quantity": r["qty"].astype(int),
                    "Refund Amount": r["amount_local"].astype(float),
                    "Currency": r["currency"].astype(str),
                    "Reason": r["reason_code"].astype(str),
                    "Refund Date": r["refund_date_local"].astype(str),
                }
            ).reset_index(drop=True),
            "ads": pd.DataFrame(
                {
                    "Campaign ID": a["campaign_id"].astype(str),
                    "Seller SKU": a["sku_id"].astype(str),
                    "Date": a["date_local"].astype(str),
                    "Impressions": a["impressions"].astype(int),
                    "Clicks": a["clicks"].astype(int),
                    "Spend": a["spend_local"].astype(float),
                    "Attributed Sales": a["attributed_sales_local"].astype(float),
                    "Currency": a["currency"].astype(str),
                }
            ).reset_index(drop=True),
            "settlement": pd.DataFrame(
                {
                    "Settlement ID": s["settlement_id"].astype(str),
                    "Order Item ID": s["order_item_id"].astype(str),
                    "Date": s["date_local"].astype(str),
                    "Fee Type": s["fee_type"].astype(str),
                    "Amount": s["amount_local"].astype(float),
                    "Currency": s["currency"].astype(str),
                }
            ).reset_index(drop=True),
            "inventory": pd.DataFrame(
                {
                    "Snapshot ID": i["snapshot_id"].astype(str),
                    "Seller SKU": i["sku_id"].astype(str),
                    "Warehouse": i["warehouse"].astype(str),
                    "Date": i["date_local"].astype(str),
                    "Available": i["available_qty"].astype(int),
                    "Inbound": i["inbound_qty"].astype(int),
                    "Reserved": i["reserved_qty"].astype(int),
                }
            ).reset_index(drop=True),
        }

        for name, frame in exports.items():
            header_row = 3 if (shop_id == "DE01" and name == "orders") else 1
            preamble = "报告生成时间,2024-08-30 09:12:33\n店铺,DE01\n" if header_row == 3 else ""
            dirty: list[dict[str, object]] = []
            if name == "orders":
                dirty = self._inject_dirty(frame, shop_id)

            if name == "inventory":
                path = out / f"{shop_id}_{name}.xlsx"
                frame.to_excel(path, index=False, engine="xlsxwriter", sheet_name="inventory")
            else:
                path = out / f"{shop_id}_{name}.csv"
                self._write_csv(
                    path,
                    frame,
                    preamble=preamble,
                    dirty_header=(shop_id == "UK01" and name == "orders"),
                )

            meta.append(
                FileMeta(
                    file=path.name,
                    shop_id=shop_id,
                    dataset=name,
                    rows=len(frame),
                    header_row=header_row,
                    dirty=dirty,
                )
            )
        return meta

    def _write_csv(
        self,
        path: Path,
        df: pd.DataFrame,
        *,
        preamble: str = "",
        dirty_header: bool = False,
    ) -> None:
        text = df.to_csv(index=False)
        lines = text.splitlines(keepends=True)
        header = lines[0]
        if dirty_header:
            header = header.replace("Quantity,", "Quantity ,").replace("Status", " Status ")
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            if preamble:
                fh.write(preamble)
            fh.write(header)
            for line in lines[1:]:
                fh.write(line)

    def _inject_dirty(self, df: pd.DataFrame, shop_id: str) -> list[dict[str, object]]:
        """Corrupt a handful of cells on the *export* frame and record exactly what was done."""
        if df.empty:
            return []
        rng = self.rng
        dirty: list[dict[str, object]] = []
        plans: list[tuple[str, str]] = []
        if shop_id == "US01":
            plans = [("D2", "Item Price"), ("D3", "Seller SKU"), ("D8", "Seller SKU")]
        elif shop_id == "DE01":
            plans = [("D1", "Order Item ID"), ("D5", "Quantity"), ("D6", "Purchase Date")]
        else:
            plans = [("D4", "Currency"), ("D2", "Item Price")]

        for code, column in plans:
            n = 1 if code == "D1" else 2
            positions = sorted(rng.choice(len(df), size=n, replace=False).tolist())
            for pos in positions:
                idx = df.index[pos]
                detail = ""
                if code == "D1":
                    dup_idx = f"dup-{idx}"
                    df.loc[dup_idx] = df.loc[idx]
                    detail = "duplicated source row"
                    # the copy lands at the end of the file, so that is its row
                    dirty.append(
                        {
                            "row": len(df),
                            "type": code,
                            "reason_code": DIRTY_TYPES[code][1],
                            "detail": detail,
                            "expect": "rejected",
                        }
                    )
                    continue
                elif code == "D2":
                    df.loc[idx, column] = None
                    detail = f"{column} is empty"
                elif code == "D3":
                    df.loc[idx, column] = "SKU-9999"
                    detail = "sku_id not in dim_sku"
                elif code == "D4":
                    df.loc[idx, column] = "USD"
                    detail = "currency != shop currency"
                elif code == "D5":
                    df.loc[idx, column] = -1
                    detail = "qty < 0"
                elif code == "D6":
                    df.loc[idx, column] = "2024-02-30 10:00:00"
                    detail = "impossible date"
                elif code == "D8":
                    df.loc[idx, column] = str(df.loc[idx, column]).lower()
                    detail = "sku case mismatch"
                dirty.append(
                    {
                        "row": int(pos) + 1,
                        "type": code,
                        "reason_code": DIRTY_TYPES[code][1],
                        "detail": detail,
                        "expect": "rejected",
                    }
                )
        if shop_id == "UK01":
            dirty.append(
                {
                    "row": 1,
                    "type": "D7",
                    "reason_code": DIRTY_TYPES["D7"][1],
                    "detail": "header has stray spaces",
                    "expect": "absorbed",
                }
            )
        return dirty


def generate(
    seed: int = 20241119,
    n_skus: int = 200,
    days: int = DAYS,
    out_dir: Path | None = None,
) -> GenerationResult:
    return Generator(seed=seed, n_skus=n_skus, days=days, out_dir=out_dir).run()
