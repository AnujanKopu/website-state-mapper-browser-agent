"""Async database engine and session factory.

Backend-agnostic: the DATABASE_URL decides SQLite vs Postgres.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from engine.db.models import Base


def create_db_engine(database_url: str) -> AsyncEngine:
    _ensure_sqlite_directory(database_url)
    return create_async_engine(database_url)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Create tables if they don't exist. (Alembic migrations can replace
    this once the schema needs to evolve in place.)"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _ensure_sqlite_directory(database_url: str) -> None:
    """SQLite won't create missing parent directories; do it ourselves."""
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
