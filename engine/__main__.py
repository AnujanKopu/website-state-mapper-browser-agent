"""FlowState CLI.

Usage:
    python -m engine capture <url> [--headed]
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from engine.capture import run_single_capture
from engine.config import Settings
from engine.storage import LocalStorage


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engine", description="FlowState engine CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser("capture", help="Capture a single URL as one state")
    capture.add_argument("url", help="URL to capture (http(s):// or file://)")
    capture.add_argument(
        "--headed", action="store_true", help="Run the browser with a visible window"
    )
    return parser


async def _cmd_capture(args: argparse.Namespace) -> int:
    settings = Settings()
    state = await run_single_capture(args.url, settings, headless=False if args.headed else None)

    store = LocalStorage(settings.data_dir)
    top = state.interactables[:10]

    print("\n=== Capture complete ===")
    print(f"run id:          {state.run_id}")
    print(f"state id:        {state.state_id}")
    print(f"fingerprint:     {state.fingerprint}")
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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "capture":
        return asyncio.run(_cmd_capture(args))
    return 1


if __name__ == "__main__":
    sys.exit(main())
