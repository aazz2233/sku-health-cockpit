"""Database engine and schema helpers."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, text

from sku_cockpit.config import Settings, get_settings
from sku_cockpit.schema import metadata


def get_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    return create_engine(
        settings.require_dsn(),
        pool_pre_ping=True,
        pool_recycle=1800,
        future=True,
    )


def init_schema(engine: Engine, *, drop: bool = False) -> None:
    """Create every table. `drop=True` wipes the database first (dev only)."""
    if drop:
        metadata.drop_all(engine)
    metadata.create_all(engine)


def ping(engine: Engine) -> tuple[str, str, str]:
    """Return (database, version, charset) — used by the environment check."""
    with engine.connect() as conn:
        row = conn.execute(text("SELECT DATABASE(), VERSION(), @@character_set_database")).one()
        return str(row[0]), str(row[1]), str(row[2])


def truncate_all(engine: Engine) -> None:
    """Empty every table, keeping the schema (used before a fresh load)."""
    with engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in reversed(metadata.sorted_tables):
            conn.execute(text(f"TRUNCATE TABLE `{table.name}`"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
