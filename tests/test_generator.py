from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from sku_cockpit.data.constants import SHOPS
from sku_cockpit.data.generator import generate


def _hash_csvs(directory: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(directory.glob("*.csv")):
        out[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def test_generation_is_deterministic(workdir: Path) -> None:
    first = generate(seed=7, n_skus=30, days=20, out_dir=workdir / "a")
    second = generate(seed=7, n_skus=30, days=20, out_dir=workdir / "b")
    assert _hash_csvs(first.output_dir) == _hash_csvs(second.output_dir)


def test_different_seed_changes_output(workdir: Path) -> None:
    first = generate(seed=7, n_skus=30, days=20, out_dir=workdir / "a")
    other = generate(seed=8, n_skus=30, days=20, out_dir=workdir / "b")
    assert _hash_csvs(first.output_dir) != _hash_csvs(other.output_dir)


def test_manifest_records_dirty_rows(workdir: Path) -> None:
    result = generate(seed=7, n_skus=30, days=20, out_dir=workdir / "g")
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    dirty = [d for entry in manifest["files"] for d in entry["dirty"]]
    types = {d["type"] for d in dirty}
    assert {"D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8"} <= types
    rejected = [d for d in dirty if d["expect"] == "rejected"]
    absorbed = [d for d in dirty if d["expect"] == "absorbed"]
    assert rejected and absorbed


def test_prices_are_in_local_currency(workdir: Path) -> None:
    result = generate(seed=7, n_skus=30, days=20, out_dir=workdir / "g")
    us = pd.read_csv(result.output_dir / "US01_orders.csv", encoding="utf-8-sig")
    de = pd.read_csv(result.output_dir / "DE01_orders.csv", encoding="utf-8-sig", skiprows=2)
    # a USD price must not look like a CNY price (the 7x conversion bug)
    assert us["Item Price"].astype(float).median() < 200
    assert de["Item Price"].astype(float).median() < 200


def test_every_anomaly_is_labelled_for_every_shop(workdir: Path) -> None:
    result = generate(seed=7, n_skus=30, days=20, out_dir=workdir / "g")
    labels = result.labels
    assert set(labels["shop_id"]) == {s.shop_id for s in SHOPS}
    # a SKU may carry two anomaly types, but each type is labelled once per shop
    per_type = labels.groupby(["sku_id", "anomaly_type"]).size()
    assert (per_type == len(SHOPS)).all()
    assert len(labels) % len(SHOPS) == 0

