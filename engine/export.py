"""Graph export: serialize a run's states and edges to a JSON document.

The shape mirrors what the React Flow UI (M3) will consume, so the export
is also the API contract. The same module also builds the deterministic
context pack (Slice 4): a heuristics-only, LLM-free site description meant
to be handed to a downstream agent.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine.db import models as db

# State types that mark a navigational boundary worth calling out explicitly.
_BOUNDARY_TYPES = {
    "risky_terminal": "actions blocked by safety policy",
    "paywall": "payment / subscription wall",
    "auth_wall": "authentication required",
    "dead_end": "no outgoing actions",
    "external": "external origin (not crawled)",
}
_REGION_ORDER = ["nav", "header", "main", "aside", "modal", "footer", None]

_CTA_TAGS = {"a", "button"}
_MAX_CTAS = 8


def _visible_ctas(interactables: list[dict]) -> list[str]:
    ctas = []
    for item in interactables:
        if item.get("tag") in _CTA_TAGS:
            label = item.get("text") or item.get("aria_label") or item.get("title")
            if label:
                ctas.append(label)
        if len(ctas) >= _MAX_CTAS:
            break
    return ctas


def _label_of(item: dict) -> str:
    direct = (
        item.get("text")
        or item.get("aria_label")
        or item.get("associated_label")
        or item.get("placeholder")
        or item.get("title")
        or item.get("icon_label")
        or item.get("component_label")
    )
    if direct:
        return direct
    if item.get("context_label"):
        verb = (
            "Open"
            if item.get("kind") in {"button", "menuitem", "disclosure"}
            else "View"
        )
        return f"{verb} {item['context_label']}"
    return item.get("test_id") or item.get("href") or f"<{item.get('tag', '?')}>"


def _surface_items(interactables: list[dict]) -> list[dict]:
    """Viewport-grounded surface items with discovery + exploration metadata."""
    return [
        {
            "item_id": item.get("item_id"),
            "label": _label_of(item),
            "kind": item.get("kind"),
            "region": item.get("region"),
            "fold": item.get("fold", 0),
            "group_id": item.get("group_id"),
            "status": item.get("status", "pending"),
            "href": item.get("href"),
            "tag": item.get("tag"),
            "role": item.get("role"),
            "in_nav": item.get("in_nav", False),
            "in_form": item.get("in_form", False),
            "in_modal": item.get("in_modal", False),
            "aria_selected": item.get("aria_selected"),
            "aria_expanded": item.get("aria_expanded"),
            "aria_controls": item.get("aria_controls"),
            "aria_haspopup": item.get("aria_haspopup"),
            "aria_pressed": item.get("aria_pressed"),
            "checked": item.get("checked"),
            "placeholder": item.get("placeholder"),
            "name": item.get("name"),
            "associated_label": item.get("associated_label"),
            "input_type": item.get("input_type"),
            "required": item.get("required", False),
            "autocomplete": item.get("autocomplete"),
            "form_action": item.get("form_action"),
            "form_method": item.get("form_method"),
            "control_key": item.get("control_key", ""),
            "container_key": item.get("container_key"),
            "container_type": item.get("container_type"),
            "controlled_surface": item.get("controlled_surface"),
            "component_key": item.get("component_key"),
            "component_label": item.get("component_label"),
            "icon_label": item.get("icon_label"),
            "probe_reason": item.get("probe_reason"),
            "interaction_scope": item.get("interaction_scope", "unknown"),
            "execution_policy": item.get("execution_policy", "inventory_only"),
            "safety_category": item.get("safety_category"),
            "page_box": item.get("page_box"),
        }
        for item in interactables
    ]


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
                    .order_by(db.StateNode.created_at, db.StateNode.id)
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
                "index": index,
                "type": node.state_type,
                "url": node.url,
                "url_normalized": node.url_normalized,
                "title": node.title,
                "label": node.label,
                "summary": node.summary,
                "fingerprint": node.fingerprint,
                "depth": node.depth,
                "parent_state_id": node.parent_state_id,
                "screenshot": node.screenshot_path,
                "dom_snapshot": node.dom_snapshot_path,
                "visible_ctas": _visible_ctas(node.interactables),
                "surface_items": _surface_items(node.interactables),
                "exploration": {
                    key: value
                    for key, value in (node.exploration or {}).items()
                    if key != "evidence"
                },
                "evidence": (node.exploration or {}).get("evidence", {}),
                "flags": node.detected_flags,
                "path": node.path,
            }
            for index, node in enumerate(states)
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
                "via": edge.via,
                "surface_item_id": edge.surface_item_id,
                "transition_key": edge.transition_key or edge.id,
                "transition_kind": edge.transition_kind or edge.action_type,
                "scope": edge.scope or "local",
                "reversible": bool(edge.reversible),
                "provenance": edge.provenance or [edge.via],
                "evidence": edge.evidence or [],
            }
            for edge in edges
        ],
    }


def write_graph_json(graph: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(graph, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Context pack (Slice 4): deterministic, heuristics-only site description
# --------------------------------------------------------------------------


def _state_display(state: dict) -> str:
    return state.get("label") or state.get("title") or state.get("url_normalized") or "(untitled)"


def _surface_groups(state: dict) -> dict[str, list[dict]]:
    """Surface items grouped by region, collapsing sibling groups to one row."""
    grouped: dict[str, list[dict]] = {}
    seen_group: dict[str, dict] = {}
    for item in state.get("surface_items", []):
        region = item.get("region")
        group_id = item.get("group_id")
        if group_id and group_id in seen_group:
            seen_group[group_id]["count"] += 1
            continue
        row = {
            "label": item["label"],
            "kind": item.get("kind"),
            "status": item.get("status", "pending"),
            "fold": item.get("fold", 0),
            "count": 1,
        }
        if group_id:
            seen_group[group_id] = row
        grouped.setdefault(region, []).append(row)
    return grouped


def _compact_evidence(evidence: dict) -> dict:
    """Bound rich graph evidence for the agent-oriented context pack."""
    forms = evidence.get("forms") or []
    visuals = evidence.get("visuals") or []
    hints = evidence.get("substate_hints") or []
    return {
        "page": evidence.get("page") or {},
        "forms": [
            {**form, "fields": (form.get("fields") or [])[:20]} for form in forms[:10]
        ],
        "visuals": visuals[:15],
        "substate_hints": hints[:20],
        "counts": {
            "forms": len(forms),
            "visuals": len(visuals),
            "substate_hints": len(hints),
        },
    }


def build_context_pack(graph: dict) -> dict:
    """Turn an exported graph document into the structured context pack."""
    states = graph["states"]
    edges = graph["edges"]
    index_of = {s["id"]: i for i, s in enumerate(states)}

    out_edges: dict[str, list[dict]] = {s["id"]: [] for s in states}
    in_edges: dict[str, list[dict]] = {s["id"]: [] for s in states}
    for edge in edges:
        if edge["from"] in out_edges:
            out_edges[edge["from"]].append(edge)
        if edge["to"] in in_edges:
            in_edges[edge["to"]].append(edge)

    def transition(edge: dict, endpoint: str) -> dict:
        other = edge[endpoint]
        return {
            "state_index": index_of.get(other),
            "label": edge.get("label"),
            "via": edge.get("via", "performed"),
        }

    root = next((s for s in states if s.get("depth") == 0), states[0] if states else None)

    pack_states = []
    unexplored = []
    action_paths = []
    for i, state in enumerate(states):
        exploration = {
            key: value
            for key, value in (state.get("exploration") or {}).items()
            if key != "evidence"
        }
        pack_states.append(
            {
                "index": i,
                "id": state["id"],
                "label": _state_display(state),
                "type": state.get("type"),
                "url": state.get("url"),
                "depth": state.get("depth", 0),
                "parent_index": index_of.get(state.get("parent_state_id")),
                "flags": state.get("flags") or {},
                "screenshot": state.get("screenshot"),
                "surface_groups": _surface_groups(state),
                "in_transitions": [transition(e, "from") for e in in_edges[state["id"]]],
                "out_transitions": [transition(e, "to") for e in out_edges[state["id"]]],
                "exploration": exploration,
                "evidence": _compact_evidence(state.get("evidence") or {}),
            }
        )
        if state.get("path"):
            action_paths.append({"state_index": i, "steps": state["path"]})
        pending_items = [
            {"label": it["label"], "kind": it.get("kind"), "region": it.get("region")}
            for it in state.get("surface_items", [])
            if it.get("status") == "pending"
        ]
        if pending_items:
            unexplored.append(
                {
                    "state_index": i,
                    "label": _state_display(state),
                    "pending_items": pending_items,
                }
            )

    boundaries = [
        {
            "state_index": i,
            "label": _state_display(s),
            "type": s.get("type"),
            "reason": _BOUNDARY_TYPES[s["type"]],
        }
        for i, s in enumerate(states)
        if s.get("type") in _BOUNDARY_TYPES
    ]

    entry_flows = []
    if root is not None:
        for edge in sorted(
            out_edges[root["id"]], key=lambda e: e.get("confidence") or 0.0, reverse=True
        )[:8]:
            entry_flows.append(
                {
                    "label": edge.get("label"),
                    "to_index": index_of.get(edge["to"]),
                    "via": edge.get("via", "performed"),
                }
            )

    stats = graph["run"].get("stats") or {}
    return {
        "run": graph["run"],
        "site_summary": {
            "entry_url": graph["run"].get("url"),
            "state_count": len(states),
            "edge_count": len(edges),
            "inferred_edge_count": sum(1 for e in edges if e.get("via") == "inferred"),
            "page_type_inventory": dict(Counter(s.get("type") for s in states)),
            "entry_flows": entry_flows,
            "boundaries": boundaries,
            "pending_actions": stats.get("pending_actions", 0),
            "pending_states": stats.get("pending_states", 0),
            "stop_reason": stats.get("stop_reason"),
        },
        "states": pack_states,
        "adjacency": [
            {
                "from_index": index_of.get(e["from"]),
                "to_index": index_of.get(e["to"]),
                "label": e.get("label"),
                "via": e.get("via", "performed"),
            }
            for e in edges
        ],
        "action_paths": action_paths,
        "unexplored": unexplored,
    }


def _render_path(steps: list[dict]) -> str:
    parts = []
    for step in steps:
        if step.get("kind") == "goto":
            parts.append(f"goto {step.get('url')}")
        else:
            parts.append(f"click {step.get('label') or step.get('selector')!r}")
    return " -> ".join(parts)


def render_context_markdown(pack: dict) -> str:
    """Render the context pack as human- and LLM-readable Markdown."""
    summary = pack["site_summary"]
    run = pack["run"]
    lines: list[str] = []

    lines.append(f"# FlowState context pack: {summary['entry_url']}")
    lines.append("")
    lines.append(
        f"> status **{run.get('status')}** · "
        f"generated from run `{run.get('id')}` · "
        f"finished {run.get('finished_at') or 'n/a'}"
    )
    lines.append("")

    lines.append("## Site summary")
    inventory = ", ".join(f"{n}× {t}" for t, n in summary["page_type_inventory"].items())
    lines.append(f"- **Entry**: {summary['entry_url']}")
    lines.append(f"- **States**: {summary['state_count']} ({inventory})")
    lines.append(
        f"- **Edges**: {summary['edge_count']} "
        f"({summary['inferred_edge_count']} inferred / no-click)"
    )
    lines.append(
        f"- **Coverage**: {summary['pending_actions']} pending action(s) across "
        f"{summary['pending_states']} state(s)"
        + (f" · stopped: {summary['stop_reason']}" if summary["stop_reason"] else "")
    )
    if summary["entry_flows"]:
        flows = "; ".join(
            f"{f['label']} (-> s{f['to_index']})"
            + (" [inferred]" if f["via"] == "inferred" else "")
            for f in summary["entry_flows"]
        )
        lines.append(f"- **Entry flows**: {flows}")
    if summary["boundaries"]:
        lines.append("- **Boundaries**:")
        for b in summary["boundaries"]:
            lines.append(f"  - s{b['state_index']} {b['label']} — {b['type']} ({b['reason']})")
    lines.append("")

    lines.append("## States")
    for s in pack["states"]:
        header = f"### s{s['index']} — {s['label']} [{s['type']}] (depth {s['depth']})"
        lines.append(header)
        lines.append(f"- URL: {s['url']}")
        if s["parent_index"] is not None:
            lines.append(f"- Sub-state of: s{s['parent_index']}")
        active_flags = [k for k, v in s["flags"].items() if v is True]
        if active_flags:
            lines.append(f"- Flags: {', '.join(active_flags)}")
        evidence = s.get("evidence") or {}
        forms = evidence.get("forms") or []
        if forms:
            rendered_forms = []
            for form in forms:
                fields = ", ".join(
                    field.get("label") or field.get("name") or field.get("type") or field["tag"]
                    for field in form.get("fields", [])
                )
                rendered_forms.append(
                    f"{form.get('label') or form.get('method', 'get').upper() + ' form'}"
                    + (f" ({fields})" if fields else "")
                )
            lines.append(f"- Forms: {'; '.join(rendered_forms)}")
        visuals = evidence.get("visuals") or []
        if visuals:
            lines.append(
                "- Visual content: "
                + ", ".join(
                    f"{item.get('kind')}"
                    + (f" ({item.get('label')})" if item.get("label") else "")
                    for item in visuals
                )
            )
        hints = evidence.get("substate_hints") or []
        if hints:
            lines.append(
                "- Possible substates: "
                + ", ".join(
                    f"{item.get('label')} [{item.get('kind') or item.get('popup') or 'control'}]"
                    for item in hints
                )
            )
        for region in _REGION_ORDER:
            rows = s["surface_groups"].get(region)
            if not rows:
                continue
            label = region or "other"
            rendered = ", ".join(
                r["label"] + (f" (×{r['count']})" if r["count"] > 1 else "")
                + ("" if r["status"] == "explored" else f" [{r['status']}]")
                for r in rows
            )
            lines.append(f"- {label}: {rendered}")
        if s["out_transitions"]:
            outs = "; ".join(
                f"s{t['state_index']} ({t['label']})"
                + (" [inferred]" if t["via"] == "inferred" else "")
                for t in s["out_transitions"]
            )
            lines.append(f"- Out: {outs}")
        if s["in_transitions"]:
            ins = "; ".join(
                f"s{t['state_index']} ({t['label']})" for t in s["in_transitions"]
            )
            lines.append(f"- In: {ins}")
        if s.get("screenshot"):
            lines.append(f"- Screenshot: `{s['screenshot']}`")
        lines.append("")

    lines.append("## Adjacency")
    for a in pack["adjacency"]:
        suffix = " [inferred]" if a["via"] == "inferred" else ""
        lines.append(f"- s{a['from_index']} -> s{a['to_index']}: {a['label']}{suffix}")
    lines.append("")

    lines.append("## Action paths")
    for p in pack["action_paths"]:
        lines.append(f"- s{p['state_index']}: {_render_path(p['steps'])}")
    lines.append("")

    lines.append("## Unexplored frontier")
    if not pack["unexplored"]:
        lines.append("- (none — every discovered affordance was visited)")
    for u in pack["unexplored"]:
        items = ", ".join(
            f"{it['label']}" + (f" ({it['kind']})" if it.get("kind") else "")
            for it in u["pending_items"]
        )
        lines.append(f"- s{u['state_index']} {u['label']}: {items}")
    lines.append("")

    return "\n".join(lines)


async def export_context_pack(
    session_factory: async_sessionmaker[AsyncSession], run_id: str
) -> dict:
    """Build both representations of the context pack for a run."""
    graph = await export_graph(session_factory, run_id)
    pack = build_context_pack(graph)
    return {"json": pack, "markdown": render_context_markdown(pack)}
