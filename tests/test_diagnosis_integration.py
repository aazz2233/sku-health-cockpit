"""Integration checks for the diagnosis layer (needs MySQL + detected events)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from sku_cockpit.config import get_settings
from sku_cockpit.db import get_engine
from sku_cockpit.diagnosis import DiagnosisOut, build_metric_pack, select_events, validate_citations

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def engine():  # type: ignore[no-untyped-def]
    try:
        eng = get_engine(get_settings())
        with eng.connect() as conn:
            n = conn.execute(text("SELECT COUNT(*) FROM anomaly_event")).scalar_one()
    except (OperationalError, RuntimeError) as exc:  # pragma: no cover
        pytest.skip(f"MySQL not available: {exc}")
    if int(n) == 0:
        pytest.skip("no anomaly events; run `python -m sku_cockpit detect` first")
    return eng


def test_selection_is_deduped_per_sku_day(engine) -> None:  # type: ignore[no-untyped-def]
    events = select_events(engine, limit=200)
    keys = [(e["sku_id"], e["shop_id"], e["date_local"]) for e in events]
    assert keys, "selection returned nothing"
    assert len(keys) == len(set(keys))


def test_selection_only_covers_p0_p1(engine) -> None:  # type: ignore[no-untyped-def]
    events = select_events(engine, limit=200)
    assert {e["severity"] for e in events} <= {"P0", "P1"}


def test_metric_pack_contains_core_metrics(engine) -> None:  # type: ignore[no-untyped-def]
    event = select_events(engine, limit=1)[0]
    pack = build_metric_pack(engine, event)
    for key in ("rev_30d", "net_margin_30d", "ad_spend_30d", "refund_rate_30d"):
        assert key in pack, key
        assert isinstance(pack[key]["value"], (int, float, str))


def test_citing_whitelisted_values_passes_validation(engine) -> None:  # type: ignore[no-untyped-def]
    event = select_events(engine, limit=1)[0]
    pack = build_metric_pack(engine, event)
    margin = pack["net_margin_30d"]["value"]
    out = DiagnosisOut.model_validate(
        {
            "summary": "test",
            "likely_causes": [],
            "recommended_actions": [],
            "need_human_review": True,
            "missing_info": [],
            "cited_metrics": [{"metric_id": "net_margin_30d", "value": float(margin)}],
        }
    )
    assert validate_citations(out, pack) == []
