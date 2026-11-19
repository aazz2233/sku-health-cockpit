from __future__ import annotations

from pathlib import Path

import pytest

from sku_cockpit.data.mapping import (
    DatasetSpec,
    MappingError,
    load_shop_mapping,
    normalize_header,
    read_dataset,
    resolve_columns,
)


def test_header_normalisation_absorbs_dirty_headers() -> None:
    assert normalize_header("  Quantity   ") == "quantity"
    assert normalize_header("\ufeffOrder ID") == "order id"
    assert normalize_header(" Status \n") == "status"


def test_resolve_columns_handles_alias_order() -> None:
    spec = DatasetSpec(
        dataset="orders",
        file="x.csv",
        header_row=1,
        columns={"qty": ("Quantity", "数量")},
    )
    assert resolve_columns([" 数量 "], spec) == {"qty": " 数量 "}


def test_missing_column_raises_with_alias_list() -> None:
    spec = DatasetSpec(
        dataset="orders",
        file="x.csv",
        header_row=1,
        columns={"qty": ("Quantity", "数量")},
    )
    with pytest.raises(MappingError, match="数量"):
        resolve_columns(["Qty"], spec)


def test_extra_source_column_raises() -> None:
    spec = DatasetSpec(
        dataset="orders",
        file="x.csv",
        header_row=1,
        columns={"qty": ("Quantity",)},
    )
    with pytest.raises(MappingError, match="not covered"):
        resolve_columns(["Quantity", "Mystery"], spec)


def test_read_dataset_skips_preamble_rows(workdir: Path) -> None:
    path = workdir / "de.csv"
    path.write_text(
        "generated at,2024-08-30\nshop,DE01\nOrder Item ID,Quantity\nA-1,2\n",
        encoding="utf-8",
    )
    spec = DatasetSpec(
        dataset="orders",
        file="de.csv",
        header_row=3,
        columns={"order_item_id": ("Order Item ID",), "qty": ("Quantity",)},
    )
    frame = read_dataset(path, spec)
    assert frame["order_item_id"].tolist() == ["A-1"]
    assert frame["_row"].tolist() == [1]


def test_all_shipped_mappings_load() -> None:
    for shop in ("US01", "DE01", "UK01"):
        specs = load_shop_mapping(shop)
        assert set(specs) == {"orders", "refunds", "ads", "settlement", "inventory"}
        for spec in specs.values():
            assert spec.file.startswith(shop)

