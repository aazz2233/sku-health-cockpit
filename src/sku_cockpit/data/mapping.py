"""Column mapping: turn shop-specific exports into canonical field names.

The mapping is *data* (config/mapping/*.yaml), never code. Adding a shop means
adding a YAML file, not touching Python.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yaml

from sku_cockpit.config import ROOT, get_settings


class MappingError(RuntimeError):
    """A source file does not match its declared mapping."""


@dataclass(frozen=True)
class DatasetSpec:
    dataset: str
    file: str
    header_row: int
    columns: dict[str, tuple[str, ...]]
    date_formats: tuple[str, ...] = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y")

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(self.columns)

    def aliases(self, canonical: str) -> tuple[str, ...]:
        return self.columns[canonical]


def normalize_header(name: object) -> str:
    """Collapse whitespace, strip, lowercase. Defeats D7 (dirty headers)."""
    text = str(name).replace("\ufeff", "")
    return re.sub(r"\s+", " ", text).strip().lower()


def load_shop_mapping(shop_id: str, config_dir: Path | None = None) -> dict[str, DatasetSpec]:
    config_dir = config_dir or (ROOT / "config" / "mapping")
    path = config_dir / f"{shop_id.lower()}.yaml"
    if not path.exists():
        raise MappingError(f"no mapping file for shop {shop_id}: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    specs: dict[str, DatasetSpec] = {}
    for dataset, body in raw["datasets"].items():
        specs[dataset] = DatasetSpec(
            dataset=dataset,
            file=body["file"],
            header_row=int(body.get("header_row", 1)),
            columns={k: tuple(v) for k, v in body["columns"].items()},
            date_formats=tuple(body.get("date_formats", ["%Y-%m-%d %H:%M:%S"])),
        )
    return specs


def resolve_columns(actual: list[str], spec: DatasetSpec) -> dict[str, str]:
    """Map canonical -> actual header. Raises on unknown or missing columns."""
    by_norm = {normalize_header(h): h for h in actual}
    resolved: dict[str, str] = {}
    for canonical in spec.columns:
        for alias in spec.aliases(canonical):
            hit = by_norm.get(normalize_header(alias))
            if hit is not None:
                resolved[canonical] = hit
                break
        else:
            raise MappingError(
                f"{spec.file}: required column '{canonical}' not found "
                f"(aliases tried: {', '.join(spec.aliases(canonical))})"
            )
    unmapped = [
        h
        for h in actual
        if normalize_header(h)
        not in {normalize_header(a) for c in spec.columns for a in spec.aliases(c)}
    ]
    if unmapped:
        raise MappingError(f"{spec.file}: source columns not covered by mapping: {unmapped}")
    return resolved


def read_dataset(path: Path, spec: DatasetSpec) -> pd.DataFrame:
    """Read a source file with raw string dtypes (validation does the typing)."""
    if not path.exists():
        raise MappingError(f"source file missing: {path}")
    if path.suffix.lower() in {".xlsx", ".xls"}:
        frame = pd.read_excel(path, sheet_name=0, skiprows=spec.header_row - 1, dtype=str)
    else:
        frame = pd.read_csv(
            path,
            skiprows=spec.header_row - 1,
            dtype=str,
            keep_default_na=True,
            encoding="utf-8-sig",
        )
    if frame.empty:
        raise MappingError(f"{path.name}: no data rows after header at line {spec.header_row}")
    resolved = resolve_columns([str(c) for c in frame.columns], spec)
    out = pd.DataFrame({canonical: frame[actual] for canonical, actual in resolved.items()})
    out["_row"] = range(1, len(out) + 1)  # 1-based data row, manifest-compatible
    return out


def shop_id_from_filename(name: str) -> str | None:
    match = re.match(r"^(US01|DE01|UK01)_", name)
    return match.group(1) if match else None


def default_generated_dir() -> Path:
    settings = get_settings()
    return settings.generated_dir / f"seed{settings.data_seed}"
