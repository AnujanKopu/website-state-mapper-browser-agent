"""Graph export: serialize a run's states and edges to a JSON document.

The shape mirrors what the React Flow UI (M3) will consume, so the export
is also the API contract.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine.db import models as db

_CTA_TAGS = {"a", "button"}
_MAX_CTAS = 8


def _visible_ctas(interactables: list[dict]) -> list[str]:
    ctas = []
    for item in interactables:
        if item.get("tag") in _CTA_TAGS:
            label = item.get("text") or item.get("aria_label")
            if label:
                ctas.append(label)
        if len(ctas) >= _MAX_CTAS:
            break
    return ctas


async def export_graph(
    session_factory: async_sessionmaker[AsyncSession], run_id: str
) -> dict:
    """Build the full graph document for a run."""
    async with session_factory() as session:
        run = await session.get(db.Run, run_id)
        if run is None:
            raise ValueError(f"Unknown run: {run_id}")
        states = (
            (
                await session.execute(
                    select(db.StateNode)
                    .where(db.StateNode.run_id == run_id)
                    .order_by(db.StateNode.created_at)
                )
            )
            .scalars()
            .all()
        )
        edges = (
            (
                await session.execute(
                    select(db.Edge)
                    .where(db.Edge.run_id == run_id)
                    .order_by(db.Edge.created_at)
                )
            )
            .scalars()
            .all()
        )

    return {
        "run": {
            "id": run.id,
            "url": run.url,
            "status": run.status,
            "stats": run.stats,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        },
        "states": [
            {
                "id": node.id,
                "type": node.state_type,
                "url": node.url,
                "url_normalized": node.url_normalized,
                "title": node.title,
                "label": node.label,
                "summary": node.summary,
                "fingerprint": node.fingerprint,
                "depth": node.depth,
                "screenshot": node.screenshot_path,
                "dom_snapshot": node.dom_snapshot_path,
                "visible_ctas": _visible_ctas(node.interactables),
                "flags": node.detected_flags,
                "path": node.path,
            }
            for node in states
        ],
        "edges": [
            {
                "id": edge.id,
                "from": edge.from_state_id,
                "to": edge.to_state_id,
                "action": edge.action_type,
                "label": edge.label,
                "selector": edge.selector,
                "element_text": edge.element_text,
                "confidence": edge.confidence,
                "collapsed_count": edge.collapsed_count,
            }
            for edge in edges
        ],
    }


def write_graph_json(graph: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(graph, indent=2), encoding="utf-8")
