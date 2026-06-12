"""Context pack (Slice 4): deterministic, LLM-free site description.

A fast unit test over a synthetic graph covers the structure and rendering;
one integration test drives a real exploration of the fixture site and
asserts the pack actually describes its modal, tabs, pricing and the
checkout boundary, plus any unexplored items.
"""

from engine.config import Settings
from engine.db.session import create_db_engine, create_session_factory
from engine.explorer import Explorer
from engine.export import (
    build_context_pack,
    export_context_pack,
    render_context_markdown,
)
from engine.schemas import BrowserConfig, BudgetConfig, RunConfig
from tests.conftest import FIXTURE_SITE

_SYNTHETIC_GRAPH = {
    "run": {
        "id": "run123",
        "url": "https://demo.test/",
        "status": "done",
        "finished_at": "2026-01-01T00:00:00+00:00",
        "stats": {"pending_actions": 2, "pending_states": 1, "stop_reason": "frontier_exhausted"},
    },
    "states": [
        {
            "id": "a",
            "type": "page",
            "url": "https://demo.test/",
            "url_normalized": "https://demo.test/",
            "title": "Home",
            "label": None,
            "depth": 0,
            "parent_state_id": None,
            "screenshot": "runs/run123/a.png",
            "flags": {"dead_end": False},
            "exploration": {"pending": 2, "explored": 1, "visit_status": "partially_explored"},
            "surface_items": [
                {
                    "item_id": "i1", "label": "Pricing", "kind": "link", "region": "nav",
                    "fold": 0, "group_id": "g1", "status": "explored",
                },
                {
                    "item_id": "i2", "label": "Sign up", "kind": "button", "region": "main",
                    "fold": 0, "group_id": "g2", "status": "pending",
                },
                {
                    "item_id": "i3", "label": "Read post", "kind": "link", "region": "main",
                    "fold": 1, "group_id": "g3", "status": "pending",
                },
                {
                    "item_id": "i4", "label": "Read post", "kind": "link", "region": "main",
                    "fold": 1, "group_id": "g3", "status": "pending",
                },
            ],
            "path": [{"kind": "goto", "url": "https://demo.test/"}],
        },
        {
            "id": "b",
            "type": "risky_terminal",
            "url": "https://demo.test/checkout",
            "url_normalized": "https://demo.test/checkout",
            "title": "Checkout",
            "label": None,
            "depth": 1,
            "parent_state_id": None,
            "screenshot": "runs/run123/b.png",
            "flags": {"payment_required": True},
            "exploration": {"pending": 0, "visit_status": "fully_explored"},
            "surface_items": [],
            "path": [{"kind": "goto", "url": "https://demo.test/checkout"}],
        },
    ],
    "edges": [
        {
            "id": "e1", "from": "a", "to": "b", "action": "click",
            "label": "Clicked 'Pricing'", "via": "performed", "confidence": 0.6,
            "surface_item_id": "i1",
        },
    ],
}


def test_build_context_pack_structure():
    pack = build_context_pack(_SYNTHETIC_GRAPH)

    summary = pack["site_summary"]
    assert summary["state_count"] == 2
    assert summary["edge_count"] == 1
    assert summary["page_type_inventory"] == {"page": 1, "risky_terminal": 1}
    assert summary["pending_actions"] == 2

    # boundary detection picks up the risky checkout
    boundary_types = {b["type"] for b in summary["boundaries"]}
    assert "risky_terminal" in boundary_types

    # sibling "Read post" items collapse into one surface row (×2)
    home = pack["states"][0]
    main_rows = home["surface_groups"]["main"]
    read_post = next(r for r in main_rows if r["label"] == "Read post")
    assert read_post["count"] == 2

    # unexplored frontier carries the pending items
    assert pack["unexplored"]
    pending_labels = {it["label"] for it in pack["unexplored"][0]["pending_items"]}
    assert "Sign up" in pending_labels

    # adjacency + action paths present
    assert pack["adjacency"][0] == {
        "from_index": 0, "to_index": 1, "label": "Clicked 'Pricing'", "via": "performed"
    }
    assert pack["action_paths"][0]["state_index"] == 0


def test_render_context_markdown_is_readable():
    markdown = render_context_markdown(build_context_pack(_SYNTHETIC_GRAPH))

    assert "# FlowState context pack: https://demo.test/" in markdown
    assert "## Site summary" in markdown
    assert "## States" in markdown
    assert "## Adjacency" in markdown
    assert "## Unexplored frontier" in markdown
    # collapsed siblings and pending status are surfaced in the prose
    assert "Read post (×2)" in markdown
    assert "Sign up" in markdown
    # the risky terminal is called out as a boundary
    assert "risky_terminal" in markdown


async def test_context_pack_describes_fixture_site(settings: Settings):
    config = RunConfig(
        browser=BrowserConfig(stabilize_quiet_ms=50),
        budgets=BudgetConfig(max_states=40, max_actions=80, max_depth=3, max_wall_seconds=180),
    )
    explorer = Explorer(settings, config)
    run_id = await explorer.run((FIXTURE_SITE / "index.html").as_uri())

    engine = create_db_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    pack = await export_context_pack(session_factory, run_id)
    await engine.dispose()

    md = pack["markdown"]
    data = pack["json"]

    # describes the structural variety the fixture exercises
    inventory = data["site_summary"]["page_type_inventory"]
    assert inventory.get("modal", 0) >= 1
    assert inventory.get("page", 0) >= 1
    assert any(t in inventory for t in ("tab", "dropdown"))

    # the checkout is reported as a boundary
    assert any(b["type"] == "risky_terminal" for b in data["site_summary"]["boundaries"])

    # markdown is non-trivial and mentions pricing + checkout
    assert "## States" in md
    assert "pricing" in md.lower()
    assert "checkout" in md.lower()

    # every state row keeps a replayable path
    assert all(p["steps"] for p in data["action_paths"])
