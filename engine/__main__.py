"""FlowState CLI.

Usage:
    python -m engine capture <url> [--headed]
    python -m engine explore <url> [--out graph.json] [--headed]
                                   [--max-states N] [--max-actions N] [--max-depth N]
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import Counter
from pathlib import Path

from engine.capture import run_single_capture
from engine.config import Settings, load_run_config
from engine.db.session import create_db_engine, create_session_factory
from engine.explorer import Explorer, ExplorerEvent
from engine.export import export_graph, write_graph_json
from engine.storage import LocalStorage


_URL_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)


def _to_url(value: str) -> str:
    """Accept URLs, local file paths, or bare hostnames as the target."""
    if _URL_SCHEME.match(value):
        return value
    path = Path(value)
    if path.exists():
        return path.resolve().as_uri()
    return f"https://{value}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engine", description="FlowState engine CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser("capture", help="Capture a single URL as one state")
    capture.add_argument("url", help="URL to capture (http(s):// or file://)")
    capture.add_argument(
        "--headed", action="store_true", help="Run the browser with a visible window"
    )

    explore = subparsers.add_parser(
        "explore", help="Explore a site and build its state graph"
    )
    explore.add_argument("url", help="Root URL to explore (http(s):// or file://)")
    explore.add_argument(
        "--out", help="Output path for graph JSON (default: data/runs/<id>/graph.json)"
    )
    explore.add_argument(
        "--headed", action="store_true", help="Run the browser with a visible window"
    )
    explore.add_argument("--max-states", type=int, help="Override max states budget")
    explore.add_argument("--max-actions", type=int, help="Override max actions budget")
    explore.add_argument("--max-depth", type=int, help="Override max depth budget")
    return parser


async def _cmd_capture(args: argparse.Namespace) -> int:
    settings = Settings()
    state = await run_single_capture(
        _to_url(args.url), settings, headless=False if args.headed else None
    )

    store = LocalStorage(settings.data_dir)
    top = state.interactables[:10]

    print("\n=== Capture complete ===")
    print(f"run id:          {state.run_id}")
    print(f"state id:        {state.state_id}")
    print(f"fingerprint:     {state.fingerprint}")
    print(f"state type:      {state.state_type.value}")
    print(f"url:             {state.url}")
    print(f"normalized url:  {state.url_normalized}")
    print(f"title:           {state.title}")
    print(f"visible text:    {len(state.visible_text)} chars")
    print(f"screenshot:      {store.path_for(state.screenshot_path)}")
    print(f"dom snapshot:    {store.path_for(state.dom_snapshot_path)}")
    print(f"interactables:   {len(state.interactables)} found, showing {len(top)}")
    for item in top:
        target = f" -> {item.href}" if item.href else ""
        print(f"  [{item.tag:>7}] {item.label!r}{target}")
    print(f"\ndatabase:        {settings.database_url}")
    return 0


def _print_event(event: ExplorerEvent) -> None:
    print(f"  {event.kind:<17} {event.message}")


async def _cmd_explore(args: argparse.Namespace) -> int:
    settings = Settings()
    config = load_run_config(settings.run_config_path)
    if args.headed:
        config.browser.headless = False
    if args.max_states is not None:
        config.budgets.max_states = args.max_states
    if args.max_actions is not None:
        config.budgets.max_actions = args.max_actions
    if args.max_depth is not None:
        config.budgets.max_depth = args.max_depth

    explorer = Explorer(settings, config, on_event=_print_event)
    run_id = await explorer.run(_to_url(args.url))

    engine = create_db_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    graph = await export_graph(session_factory, run_id)
    await engine.dispose()

    out_path = (
        Path(args.out) if args.out else settings.data_dir / "runs" / run_id / "graph.json"
    )
    write_graph_json(graph, out_path)

    type_counts = Counter(state["type"] for state in graph["states"])
    print("\n=== Exploration complete ===")
    print(f"run id:     {run_id}")
    print(f"states:     {len(graph['states'])} ({dict(type_counts)})")
    print(f"edges:      {len(graph['edges'])}")
    print(f"stats:      {graph['run']['stats']}")
    print(f"graph json: {out_path.resolve()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "capture":
        return asyncio.run(_cmd_capture(args))
    if args.command == "explore":
        return asyncio.run(_cmd_explore(args))
    return 1


if __name__ == "__main__":
    sys.exit(main())
