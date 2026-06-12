"""Async database engine and session factory.

Backend-agnostic: the DATABASE_URL decides SQLite vs Postgres.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from engine.db.models import Base


def create_db_engine(database_url: str) -> AsyncEngine:
    _ensure_sqlite_directory(database_url)
    engine = create_async_engine(database_url)
    if make_url(database_url).get_backend_name() == "sqlite":
        _enable_sqlite_concurrency(engine)
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


# Columns added after early v1 databases were created; patched in place for SQLite.
_SQLITE_COLUMN_PATCHES: dict[str, list[str]] = {
    "state_nodes": [
        "parent_state_id VARCHAR(32)",
        "exploration JSON DEFAULT '{}'",
    ],
    "edges": [
        "via VARCHAR(12) DEFAULT 'performed'",
        "surface_item_id VARCHAR(16)",
    ],
}


def _patch_sqlite_schema(connection: Connection) -> None:
    """Add columns missing from older local SQLite files."""
    inspector = inspect(connection)
    for table, column_defs in _SQLITE_COLUMN_PATCHES.items():
        if not inspector.has_table(table):
            continue
        existing = {col["name"] for col in inspector.get_columns(table)}
        for col_def in column_defs:
            col_name = col_def.split()[0]
            if col_name not in existing:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_def}"))


async def init_db(engine: AsyncEngine) -> None:
    """Create tables if they don't exist. (Alembic migrations can replace
    this once the schema needs to evolve in place.)"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if make_url(engine.url).get_backend_name() == "sqlite":
            await conn.run_sync(_patch_sqlite_schema)


def _ensure_sqlite_directory(database_url: str) -> None:
    """SQLite won't create missing parent directories; do it ourselves."""
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)


def _enable_sqlite_concurrency(engine: AsyncEngine) -> None:
    """WAL journaling lets the API read the graph while the explorer writes
    it; busy_timeout avoids spurious 'database is locked' under contention."""

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()
