"""Ingest pipeline: file -> raw -> validate -> staging, with idempotency."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy import Engine, Numeric, select

from sku_cockpit.config import Settings, get_settings
from sku_cockpit.data.mapping import (
    DatasetSpec,
    MappingError,
    load_shop_mapping,
    read_dataset,
)
from sku_cockpit.data.validation import (
    VALIDATORS,
    Context,
    Reject,
    Validated,
)
from sku_cockpit.schema import (
    STAGING_TABLES,
    anomaly_label,
    dim_date,
    dim_fee_schedule,
    dim_fx,
    dim_listing,
    dim_shipment,
    dim_shop,
    dim_sku,
    ingest_run,
    raw_row,
    reject_row,
)

# Datasets whose full payload is kept in raw_row for line-level auditing.
RAW_DATASETS = {"orders", "refunds"}
DATASET_ORDER = ("orders", "refunds", "ads", "inventory", "settlement")


@dataclass
class FileOutcome:
    file: str
    dataset: str
    shop_id: str
    rows_total: int
    rows_loaded: int
    rows_rejected: int
    skipped: bool = False
    status: str = "success"
    rejects: list[Reject] = field(default_factory=list)


@dataclass
class IngestReport:
    outcomes: list[FileOutcome] = field(default_factory=list)

    @property
    def totals(self) -> dict[str, int]:
        return {
            "files": len(self.outcomes),
            "rows_total": sum(o.rows_total for o in self.outcomes),
            "rows_loaded": sum(o.rows_loaded for o in self.outcomes),
            "rows_rejected": sum(o.rows_rejected for o in self.outcomes),
            "skipped": sum(1 for o in self.outcomes if o.skipped),
        }

    def reject_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            for reject in outcome.rejects:
                counts[reject.reason] = counts.get(reject.reason, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _money(value: Any) -> Decimal:
    return Decimal(str(round(float(value), 4))).quantize(Decimal("0.0001"))


def _native(value: Any) -> Any:
    """numpy / pandas scalars -> plain Python, so JSON and MySQL drivers accept them."""
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if isinstance(value, (datetime, date)):
        return value
    if isinstance(value, str):
        return value
    return value


class Ingestor:
    def __init__(
        self,
        engine: Engine,
        settings: Settings | None = None,
        generated_dir: Path | None = None,
    ) -> None:
        self.engine = engine
        self.settings = settings or get_settings()
        self.generated_dir = generated_dir or (
            self.settings.generated_dir / f"seed{self.settings.data_seed}"
        )
        self.report = IngestReport()

    # ------------------------------------------------------------------ dims
    def load_dimensions(self) -> None:
        shops = pd.read_csv(self.generated_dir / "shops.csv", encoding="utf-8-sig")
        skus = pd.read_csv(self.generated_dir / "skus.csv", encoding="utf-8-sig")
        listings = pd.read_csv(self.generated_dir / "listings.csv", encoding="utf-8-sig")
        shipments = pd.read_csv(self.generated_dir / "shipments.csv", encoding="utf-8-sig")
        fees = pd.read_csv(self.generated_dir / "fee_schedule.csv", encoding="utf-8-sig")
        fx = pd.read_csv(self.generated_dir / "fx_rates.csv", encoding="utf-8-sig")
        calendar = pd.read_csv(self.generated_dir / "dim_date.csv", encoding="utf-8-sig")
        labels = pd.read_csv(self.generated_dir / "anomaly_labels.csv", encoding="utf-8-sig")

        for col in ("launch_date",):
            skus[col] = pd.to_datetime(skus[col]).dt.date
        shipments["arrived_at"] = pd.to_datetime(shipments["arrived_at"]).dt.date
        for col in ("effective_from", "effective_to"):
            fees[col] = pd.to_datetime(fees[col], errors="coerce").dt.date
        calendar["date"] = pd.to_datetime(calendar["date"]).dt.date
        calendar["is_promo"] = calendar["is_promo"].astype(bool)
        fx["date"] = pd.to_datetime(fx["date"]).dt.date
        labels["date_from"] = pd.to_datetime(labels["date_from"]).dt.date
        labels["date_to"] = pd.to_datetime(labels["date_to"]).dt.date
        labels["is_anomaly"] = labels["is_anomaly"].astype(bool)

        mapping = {
            "dim_shop": (dim_shop, shops),
            "dim_sku": (dim_sku, skus),
            "dim_listing": (dim_listing, listings),
            "dim_shipment": (dim_shipment, shipments),
            "dim_fee_schedule": (
                dim_fee_schedule,
                fees.drop(columns=["fee_id"], errors="ignore"),
            ),
            "dim_date": (dim_date, calendar.rename(columns={"date": "date_key"})),
            "dim_fx": (dim_fx, fx.rename(columns={"date": "date_key"})),
            "anomaly_label": (anomaly_label, labels),
        }
        with self.engine.begin() as conn:
            # dimensions are fully rebuilt from the generated snapshot, so a
            # second run must not duplicate rows (anomaly_label has no natural key)
            for target, _ in mapping.values():
                conn.execute(target.delete())
            for _name, (target, frame) in mapping.items():
                cols = {c.name for c in target.columns}
                records = [
                    {k: v for k, v in rec.items() if k in cols and not pd.isna(v)}
                    for rec in frame.to_dict("records")
                ]
                if records:
                    conn.execute(target.insert().prefix_with("IGNORE"), records)

    # ------------------------------------------------------------- ingestion
    def run(self, shops: list[str] | None = None) -> IngestReport:
        shop_ids = shops or sorted(
            {p.stem.split("_")[0] for p in self.generated_dir.glob("*_orders.csv")}
        )
        for shop_id in shop_ids:
            specs = load_shop_mapping(shop_id, self.settings.config_dir / "mapping")
            for dataset in DATASET_ORDER:
                spec = specs.get(dataset)
                if spec is None:
                    continue
                self._ingest_file(shop_id, spec)
        return self.report

    def _already_done(self, file_hash: str, dataset: str) -> bool:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(ingest_run.c.run_id).where(
                    ingest_run.c.file_hash == file_hash,
                    ingest_run.c.dataset == dataset,
                    ingest_run.c.status == "success",
                )
            ).first()
        return row is not None

    def _ingest_file(self, shop_id: str, spec: DatasetSpec) -> None:
        path = self.generated_dir / spec.file
        file_hash = sha256_of(path)

        if self._already_done(file_hash, spec.dataset):
            self.report.outcomes.append(
                FileOutcome(spec.file, spec.dataset, shop_id, 0, 0, 0, skipped=True)
            )
            return

        try:
            raw = read_dataset(path, spec)
        except MappingError as exc:
            self.report.outcomes.append(
                FileOutcome(
                    spec.file, spec.dataset, shop_id, 0, 0, 0, status=f"mapping_error: {exc}"
                )
            )
            return

        ctx = self._context(shop_id, spec.dataset)
        validator = VALIDATORS[spec.dataset]
        validated: Validated = validator(raw, ctx)
        if spec.dataset == "orders" and not validated.frame.empty:
            validated.frame["order_at_utc"] = to_utc(
                validated.frame, "order_time_local", ctx.timezone
            )

        with self.engine.begin() as conn:
            run_id = self._open_run(conn, spec, file_hash, shop_id, len(raw))
            if spec.dataset in RAW_DATASETS:
                self._store_raw(conn, run_id, spec.dataset, raw)
            if validated.rejects:
                self._store_rejects(conn, run_id, validated.rejects)
            loaded = self._store_staging(conn, run_id, shop_id, validated)
            conn.execute(
                ingest_run.update()
                .where(ingest_run.c.run_id == run_id)
                .values(
                    rows_loaded=loaded,
                    rows_rejected=len(validated.rejects),
                    status="success",
                    finished_at=datetime.now(),
                )
            )

        self.report.outcomes.append(
            FileOutcome(
                file=spec.file,
                dataset=spec.dataset,
                shop_id=shop_id,
                rows_total=len(raw),
                rows_loaded=loaded,
                rows_rejected=len(validated.rejects),
                rejects=validated.rejects,
            )
        )

    def _open_run(
        self, conn: Any, spec: DatasetSpec, file_hash: str, shop_id: str, total: int
    ) -> int:
        result = conn.execute(
            ingest_run.insert().values(
                source_file=spec.file,
                file_hash=file_hash,
                dataset=spec.dataset,
                shop_id=shop_id,
                rows_total=total,
                status="running",
            )
        )
        return int(result.inserted_primary_key[0])

    def _store_raw(self, conn: Any, run_id: int, dataset: str, raw: pd.DataFrame) -> None:
        records = []
        for rec in raw.to_dict("records"):
            payload = {str(k): _native(v) for k, v in rec.items() if k != "_row"}
            records.append(
                {
                    "run_id": run_id,
                    "dataset": dataset,
                    "row_number": int(rec["_row"]),
                    "payload": payload,
                }
            )
        for start in range(0, len(records), 5000):
            conn.execute(raw_row.insert(), records[start : start + 5000])

    def _store_rejects(self, conn: Any, run_id: int, rejects: list[Reject]) -> None:
        records = [
            {
                "run_id": run_id,
                "row_number": r.row,
                "reason_code": r.reason,
                "detail": r.detail[:255],
                "raw_row_json": {str(k): _native(v) for k, v in r.raw.items() if k != "_row"},
            }
            for r in rejects
        ]
        for start in range(0, len(records), 1000):
            conn.execute(reject_row.insert(), records[start : start + 1000])

    def _store_staging(self, conn: Any, run_id: int, shop_id: str, validated: Validated) -> int:
        table = STAGING_TABLES[validated.dataset]
        frame = validated.frame
        if frame.empty:
            return 0
        cols = {c.name for c in table.columns}
        numeric_cols = {c.name for c in table.columns if isinstance(c.type, Numeric)}
        records: list[dict[str, Any]] = []
        for rec in frame.to_dict("records"):
            row: dict[str, Any] = {
                k: _native(v) for k, v in rec.items() if k in cols and k != "_row"
            }
            row["source_run_id"] = run_id
            row["source_row"] = int(rec["_row"])
            if "shop_id" not in row:
                row["shop_id"] = shop_id
            for col in numeric_cols & row.keys():
                if row[col] is not None and not pd.isna(row[col]):
                    row[col] = _money(row[col])
            records.append(row)
        for start in range(0, len(records), 5000):
            conn.execute(table.insert().prefix_with("IGNORE"), records[start : start + 5000])
        return len(records)

    def _context(self, shop_id: str, dataset: str) -> Context:
        with self.engine.connect() as conn:
            shop = conn.execute(
                select(dim_shop.c.currency, dim_shop.c.timezone).where(
                    dim_shop.c.shop_id == shop_id
                )
            ).one()
            skus = [r[0] for r in conn.execute(select(dim_sku.c.sku_id)).all()]
            items: dict[str, tuple[int, float, str]] = {}
            if dataset == "refunds":
                from sku_cockpit.schema import staging_order_item

                rows = conn.execute(
                    select(
                        staging_order_item.c.order_item_id,
                        staging_order_item.c.qty,
                        staging_order_item.c.item_total_local,
                        staging_order_item.c.sku_id,
                    )
                ).all()
                items = {str(r[0]): (int(r[1]), float(r[2]), str(r[3])) for r in rows}
        return Context(
            shop_id=shop_id,
            currency=str(shop[0]),
            timezone=str(shop[1]),
            sku_ids=set(skus),
            order_items=items,
        )


def to_utc(frame: pd.DataFrame, column: str, timezone: str) -> pd.Series:
    tz = ZoneInfo(timezone)
    return (
        pd.to_datetime(frame[column])
        .dt.tz_localize(tz, ambiguous="NaT", nonexistent="NaT")
        .dt.tz_convert("UTC")
        .dt.tz_localize(None)
    )


def ingest_all(
    engine: Engine | None = None,
    settings: Settings | None = None,
    shops: list[str] | None = None,
) -> IngestReport:
    from sku_cockpit.db import get_engine

    engine = engine or get_engine(settings)
    ingestor = Ingestor(engine, settings)
    ingestor.load_dimensions()
    return ingestor.run(shops)
