"""Integration checks against a real MySQL instance.

Skipped automatically when the database is unreachable (e.g. GitHub Actions),
so the unit suite stays runnable anywhere.
"""

from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from sku_cockpit.config import get_settings
from sku_cockpit.data.ingest import Ingestor, ingest_all
from sku_cockpit.db import get_engine
from sku_cockpit.schema import raw_row, staging_order_item

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def engine():  # type: ignore[no-untyped-def]
    try:
        eng = get_engine(get_settings())
        with eng.connect():
            pass
    except (OperationalError, RuntimeError) as exc:  # pragma: no cover - depends on env
        pytest.skip(f"MySQL not available: {exc}")
    return eng


def _load_manifest() -> pd.DataFrame:
    settings = get_settings()
    path = settings.generated_dir / f"seed{settings.data_seed}" / "manifest.json"
    if not path.exists():
        pytest.skip("run `python -m sku_cockpit generate` first")
    return pd.read_json(path)


def test_every_staging_row_is_traceable(engine) -> None:  # type: ignore[no-untyped-def]
    with engine.connect() as conn:
        missing = conn.execute(
            select(func.count())
            .select_from(staging_order_item)
            .where(
                (staging_order_item.c.source_run_id.is_(None))
                | (staging_order_item.c.source_row.is_(None))
            )
        ).scalar_one()
        total = conn.execute(select(func.count()).select_from(staging_order_item)).scalar_one()
    assert total > 0
    assert missing == 0


def test_a_sample_order_can_be_traced_to_raw(engine) -> None:  # type: ignore[no-untyped-def]
    with engine.connect() as conn:
        sample = conn.execute(
            select(
                staging_order_item.c.source_run_id,
                staging_order_item.c.source_row,
            ).limit(1)
        ).one()
        raw = conn.execute(
            select(raw_row.c.payload).where(
                raw_row.c.run_id == sample[0], raw_row.c.row_number == sample[1]
            )
        ).one_or_none()
    assert raw is not None
    assert "order_item_id" in raw[0]


def test_reingest_is_idempotent(engine) -> None:  # type: ignore[no-untyped-def]
    settings = get_settings()
    ingestor = Ingestor(engine, settings)
    ingestor.load_dimensions()
    report = ingestor.run()
    assert report.totals["rows_loaded"] == 0
    assert report.totals["skipped"] == report.totals["files"]


def test_ingest_all_is_callable(engine) -> None:  # type: ignore[no-untyped-def]
    report = ingest_all(engine, get_settings())
    assert report.totals["files"] >= 5
