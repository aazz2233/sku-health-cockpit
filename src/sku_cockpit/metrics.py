"""Build the mart layer from SQL templates.

Design: SQL lives in `sql/`, the runner only renders Jinja variables and
executes files in order. No business logic in Python — so the SQL can be read,
explained and reviewed on its own.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Template
from sqlalchemy import Engine, text

from sku_cockpit.config import ROOT, Settings, get_settings
from sku_cockpit.schema import mart_shop_kpi, mart_sku_daily, mart_sku_monthly

SQL_DIR = ROOT / "sql"
VIEW_FILE = "10_views.sql"
MART_FILES = (
    "20_mart_sku_daily.sql",
    "30_mart_sku_monthly.sql",
    "40_mart_shop_kpi.sql",
)

MART_TABLES = {
    "mart_sku_daily": mart_sku_daily,
    "mart_sku_monthly": mart_sku_monthly,
    "mart_shop_kpi": mart_shop_kpi,
}


def load_inventory_config(path: Path | None = None) -> dict[str, Any]:
    path = path or (ROOT / "config" / "inventory.yaml")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def build_context(config: dict[str, Any]) -> dict[str, Any]:
    """Turn the YAML config into SQL fragments."""
    default = config.get("default", {})
    cats = config.get("categories", {})
    rows = [
        f"SELECT '{name}' AS category, {int(body.get('lead_time_days', default.get('lead_time_days', 30)))} AS lead_days"
        for name, body in cats.items()
    ]
    lead_time_union = (
        "(" + " UNION ALL ".join(rows) + ")" if rows else "(SELECT '' AS category, 30 AS lead_days)"
    )

    service_level = float(default.get("service_level", 0.95))
    z_map = {float(k): float(v) for k, v in (config.get("z_scores") or {}).items()}
    z_score = z_map.get(service_level)
    if z_score is None:
        z_score = min(z_map.items(), key=lambda kv: abs(kv[0] - service_level))[1]

    return {
        "lead_time_union": lead_time_union,
        "z_score": z_score,
        "overstock_days": int(default.get("overstock_days", 180)),
    }


def split_statements(sql: str) -> list[str]:
    """Split on semicolons at end of line; ignore comment-only chunks."""
    parts = re.split(r";\s*(?:\r?\n|$)", sql)
    out = []
    for part in parts:
        body = "\n".join(
            line for line in part.splitlines() if not line.strip().startswith("--")
        ).strip()
        if body:
            out.append(body)
    return out


def rebuild_marts(engine: Engine, settings: Settings | None = None) -> dict[str, int]:
    settings = settings or get_settings()
    config = load_inventory_config()
    ctx = build_context(config)

    views_sql = (SQL_DIR / VIEW_FILE).read_text(encoding="utf-8")
    with engine.begin() as conn:
        for statement in split_statements(views_sql):
            conn.execute(text(statement))
        for name in MART_TABLES:
            conn.execute(text(f"TRUNCATE TABLE `{name}`"))

    for filename in MART_FILES:
        rendered = Template((SQL_DIR / filename).read_text(encoding="utf-8")).render(**ctx)
        with engine.begin() as conn:
            for statement in split_statements(rendered):
                conn.execute(text(statement))

    counts: dict[str, int] = {}
    with engine.connect() as conn:
        for name in MART_TABLES:
            counts[name] = int(conn.execute(text(f"SELECT COUNT(*) FROM `{name}`")).scalar_one())
    return counts
