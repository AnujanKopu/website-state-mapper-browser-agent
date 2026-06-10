"""SQLAlchemy table definitions.

Schema is intentionally Postgres-compatible (plain String/JSON columns,
no SQLite-specific features) so DATABASE_URL alone switches backends.

The full graph schema (runs / states / edges) is defined now so later
milestones extend rows, not tables.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {
        dict[str, Any]: JSON,
        list[Any]: JSON,
    }


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    stats: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    states: Mapped[list[StateNode]] = relationship(back_populates="run")


class StateNode(Base):
    __tablename__ = "state_nodes"
    __table_args__ = (UniqueConstraint("run_id", "fingerprint", name="uq_state_fingerprint"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(16))
    url: Mapped[str] = mapped_column(Text)
    url_normalized: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, default="")
    state_type: Mapped[str] = mapped_column(String(24), default="page")

    # LLM-assigned in M2; nullable until then.
    label: Mapped[str | None] = mapped_column(Text, default=None)
    summary: Mapped[str | None] = mapped_column(Text, default=None)

    screenshot_path: Mapped[str] = mapped_column(Text)
    dom_snapshot_path: Mapped[str] = mapped_column(Text)

    text_hash: Mapped[str] = mapped_column(String(40))
    interactables: Mapped[list[Any]] = mapped_column(default=list)
    detected_flags: Mapped[dict[str, Any]] = mapped_column(default=dict)

    # Ordered action steps from the run's root state (replay path, M1+).
    path: Mapped[list[Any]] = mapped_column(default=list)
    depth: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[Run] = relationship(back_populates="states")


class Edge(Base):
    __tablename__ = "edges"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "from_state_id", "selector", "action_type", name="uq_edge_action"
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    from_state_id: Mapped[str] = mapped_column(ForeignKey("state_nodes.id"), index=True)
    to_state_id: Mapped[str] = mapped_column(ForeignKey("state_nodes.id"), index=True)

    action_type: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(Text, default="")
    selector: Mapped[str] = mapped_column(Text)
    selector_strategy: Mapped[str] = mapped_column(String(8), default="css")
    element_text: Mapped[str | None] = mapped_column(Text, default=None)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
