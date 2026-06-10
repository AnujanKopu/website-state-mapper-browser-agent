"""End-to-end exploration test against the fixture site.

Drives the full engine -- frontier loop, ranking, safety, dedup, sibling
collapse, budgets, export -- with real Chromium over file:// URLs and
asserts on the resulting graph structure.
"""

import json
from pathlib import Path

from engine.config import Settings
from engine.db.session import create_db_engine, create_session_factory
from engine.explorer import Explorer
from engine.export import export_graph, write_graph_json
from engine.schemas import BrowserConfig, BudgetConfig, RunConfig
from tests.conftest import FIXTURE_SITE


def _test_config() -> RunConfig:
    return RunConfig(
        browser=BrowserConfig(stabilize_quiet_ms=50),
        budgets=BudgetConfig(max_states=40, max_actions=80, max_depth=2, max_wall_seconds=180),
    )


async def _run_exploration(settings: Settings) -> dict:
    explorer = Explorer(settings, _test_config())
    run_id = await explorer.run((FIXTURE_SITE / "index.html").as_uri())

    engine = create_db_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    graph = await export_graph(session_factory, run_id)
    await engine.dispose()
    return graph


async def test_exploration_builds_state_graph(settings: Settings, tmp_path: Path):
    graph = await _run_exploration(settings)
    states, edges = graph["states"], graph["edges"]
    by_id = {s["id"]: s for s in states}

    # --- run completed within budget ---
    assert graph["run"]["status"] == "done"
    stats = graph["run"]["stats"]
    assert stats["states"] == len(states)
    assert stats["edges"] == len(edges)
    assert stats["actions_denied"] > 0

    # --- graph integrity ---
    assert all(e["from"] in by_id and e["to"] in by_id for e in edges)
    root = next(s for s in states if s["depth"] == 0)
    assert root["type"] == "page"

    # --- URL states discovered ---
    urls = {s["url_normalized"] for s in states}
    assert any(u.endswith("pricing.html") for u in urls)
    assert any(u.endswith("docs.html") for u in urls)
    assert any(u.endswith("checkout.html") for u in urls)
    assert any(u.endswith("about.html") for u in urls)

    # --- modal is a first-class state (same URL as root, different node) ---
    modal_states = [s for s in states if s["type"] == "modal"]
    assert modal_states
    assert any(s["url_normalized"] == root["url_normalized"] for s in modal_states)

    # --- modal Close loops back to root via dedup, not a duplicate node ---
    modal_ids = {s["id"] for s in modal_states}
    assert any(e["from"] in modal_ids and e["to"] == root["id"] for e in edges)

    # --- tab switch is a state: another same-URL node beyond root/modals ---
    same_url_states = [s for s in states if s["url_normalized"] == root["url_normalized"]]
    assert len(same_url_states) >= 3  # root + modal + tab/dropdown variants

    # --- payment-like terminal detected and not expanded ---
    risky = [s for s in states if s["type"] == "risky_terminal"]
    assert len(risky) == 1
    checkout = risky[0]
    assert checkout["url_normalized"].endswith("checkout.html")
    assert checkout["flags"]["payment_required"] is True
    denied_categories = {d["category"] for d in checkout["flags"]["denied_actions"]}
    assert "payment" in denied_categories
    assert all(e["from"] != checkout["id"] for e in edges)  # no out-edges

    # --- dead end detected ---
    dead_ends = [s for s in states if s["type"] == "dead_end"]
    assert dead_ends
    assert all(s["url_normalized"].endswith("about.html") for s in dead_ends)

    # --- sibling collapse: one representative post state, one x3 edge ---
    post_states = [s for s in states if "post" in s["url_normalized"]]
    assert len(post_states) == 1
    assert post_states[0]["url_normalized"].endswith("post1.html")
    assert any(e["collapsed_count"] == 3 and "Read post" in e["label"] for e in edges)

    # --- dedup: docs has one node but multiple inbound paths ---
    docs_states = [s for s in states if s["url_normalized"].endswith("docs.html")]
    assert len(docs_states) == 1
    docs_in_edges = [e for e in edges if e["to"] == docs_states[0]["id"]]
    assert len(docs_in_edges) >= 2

    # --- safety: external domain never entered, logout never followed ---
    assert not any("example.com" in s["url"] for s in states)
    assert not any("logout" in s["url"] for s in states)

    # --- every state has a screenshot artifact and a replay path ---
    for state in states:
        artifact = settings.data_dir / state["screenshot"]
        assert artifact.exists() and artifact.stat().st_size > 0
        assert state["path"], f"state {state['id']} has no replay path"
        assert state["path"][0]["kind"] == "goto"

    # --- export round-trips through JSON on disk ---
    out = tmp_path / "graph.json"
    write_graph_json(graph, out)
    reloaded = json.loads(out.read_text(encoding="utf-8"))
    assert reloaded["run"]["id"] == graph["run"]["id"]
    assert len(reloaded["states"]) == len(states)
