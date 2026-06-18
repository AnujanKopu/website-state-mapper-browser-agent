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
        budgets=BudgetConfig(max_states=40, max_actions=80, max_depth=3, max_wall_seconds=180),
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

    # --- inferred edges: a link to an already-known page costs no click ---
    inferred = [e for e in edges if e["via"] == "inferred"]
    assert inferred, "expected at least one inferred (no-click) edge"
    assert all(e["from"] in by_id and e["to"] in by_id for e in inferred)
    assert all(e["via"] in {"performed", "inferred"} for e in edges)

    # --- same-URL sub-states hang off their parent page ---
    for modal in modal_states:
        assert modal["parent_state_id"] == root["id"]
    sub_states = [s for s in states if s["parent_state_id"]]
    assert all(s["parent_state_id"] in by_id for s in sub_states)
    # tab/dropdown variants are reclassified, not left as generic pages
    assert any(s["type"] in {"tab", "dropdown"} for s in sub_states)

    # --- surface items + coverage are exported per state ---
    assert root["surface_items"], "root should expose surface items"
    assert all("status" in item and "item_id" in item for item in root["surface_items"])
    assert root["exploration"].get("visit_status") in {
        "fully_explored",
        "partially_explored",
    }
    # the risky checkout's payment buttons surface as blocked items
    blocked_labels = {
        item["label"] for item in checkout["surface_items"] if item["status"] == "blocked"
    }
    assert blocked_labels

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


async def test_early_stop_records_pending_surface_items(settings: Settings):
    """A tiny action budget leaves most affordances unexplored; those must be
    visible as pending surface items and counted in the run's pending stats."""
    config = RunConfig(
        browser=BrowserConfig(stabilize_quiet_ms=50),
        budgets=BudgetConfig(max_states=40, max_actions=1, max_depth=2, max_wall_seconds=180),
    )
    explorer = Explorer(settings, config)
    run_id = await explorer.run((FIXTURE_SITE / "index.html").as_uri())

    engine = create_db_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    graph = await export_graph(session_factory, run_id)
    await engine.dispose()

    assert graph["run"]["stats"]["stop_reason"] == "max_actions"
    assert graph["run"]["stats"]["pending_actions"] > 0
    assert graph["run"]["stats"]["pending_states"] >= 1

    root = next(s for s in graph["states"] if s["depth"] == 0)
    statuses = {item["status"] for item in root["surface_items"]}
    assert "pending" in statuses
    assert root["exploration"]["visit_status"] == "partially_explored"
    assert root["exploration"]["pending"] > 0


async def test_dynamic_route_family_merges_templates_and_preserves_variant(
    settings: Settings,
):
    """Repeated content URLs share a node only after exact structure matches."""
    config = RunConfig(
        browser=BrowserConfig(stabilize_quiet_ms=50),
        budgets=BudgetConfig(max_states=20, max_actions=20, max_depth=2, max_wall_seconds=120),
    )
    explorer = Explorer(settings, config)
    run_id = await explorer.run((FIXTURE_SITE / "family.html").as_uri())

    engine = create_db_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    graph = await export_graph(session_factory, run_id)
    await engine.dispose()

    profile_states = [
        state
        for state in graph["states"]
        if "/profiles/" in state["url"] and state["parent_state_id"] is None
    ]
    assert len(profile_states) == 2  # shared template + moderator structural variant
    assert any(state["url"].endswith("alice.html") for state in profile_states)
    assert any(state["url"].endswith("moderator.html") for state in profile_states)
    assert not any(state["url"].endswith(("bob.html", "carol.html")) for state in profile_states)

    root = next(state for state in graph["states"] if state["depth"] == 0)
    family_items = [
        item for item in root["surface_items"] if item.get("href") and "/profiles/" in item["href"]
    ]
    assert len(family_items) == 4
    assert len({item["group_id"] for item in family_items}) == 1
    assert any(item["href"].endswith("carol.html") and item["status"] == "skipped_duplicate"
               for item in family_items)

    stats = graph["run"]["stats"]
    assert stats["family_dedup_hits"] == 1
    assert stats["family_urls_skipped"] == 1
    assert stats["actions_performed"] <= 4

    family_meta = [state["exploration"] for state in profile_states]
    assert all(meta["route_family"] for meta in family_meta)
    assert all(meta["family_sampled"] == 3 for meta in family_meta)
    assert all(meta["family_skipped"] == 1 for meta in family_meta)
    assert any(edge["collapsed_count"] == 4 for edge in graph["edges"])


def test_enqueue_actions_prefers_highest_scored():
    """DFS stack must receive the top-K scored actions, not the bottom-K."""
    from engine.classify import StateAnalysis
    from engine.explorer import Explorer, Frontier, StateMeta
    from engine.ranking import ActionCandidate
    from engine.schemas import BoundingBox, Interactable, RunConfig, StateType

    box = BoundingBox(x=0, y=0, width=10, height=10)

    explorer = Explorer(Settings(), RunConfig())
    explorer._frontier = Frontier()
    explorer._visited_urls = set()
    meta = StateMeta(
        id="root",
        index=0,
        url="https://app.test/",
        url_normalized="https://app.test/",
        depth=0,
        path=[],
        state_type=StateType.PAGE,
    )

    def cand(label: str, score: float) -> ActionCandidate:
        return ActionCandidate(
            interactable=Interactable(
                selector=f"#{label}", tag="a", text=label, bounding_box=box
            ),
            score=score,
        )

    analysis = StateAnalysis(
        candidates=[],
        safe=[cand("low", 5), cand("mid", 50), cand("login", 90), cand("high", 80)],
        denied=[],
        state_type=StateType.PAGE,
        flags={},
    )
    explorer._enqueue_actions(meta, analysis)

    assert len(explorer._frontier) == 4
    first = explorer._frontier.pop()
    assert first.candidate.interactable.text == "login"


def test_url_revisit_merges_without_new_node():
    """Cross-URL returns to a known page merge by URL, not a duplicate node."""
    from engine import identity
    from engine.explorer import Explorer, StateMeta
    from engine.identity import key_for
    from engine.schemas import Observation, PageSignals, PageSnapshot, RunConfig, StateType

    explorer = Explorer(Settings(), RunConfig())
    explorer._url_to_state = {}
    explorer._identity = identity.IdentityIndex()
    login_url = "https://app.test/login"
    source = StateMeta(
        id="privacy",
        index=2,
        url="https://app.test/privacy",
        url_normalized="https://app.test/privacy",
        depth=2,
        path=[],
        state_type=StateType.PAGE,
    )
    explorer._url_to_state["https://app.test/login"] = "login"

    observation = Observation(
        snapshot=PageSnapshot(
            url=login_url,
            title="Log in",
            visible_text="password",
            html="<form></form>",
            screenshot_png=b"",
            dom_skeleton="different-csrf-token",
            signals=PageSignals(password_fields=1),
        ),
        interactables=[],
        url_normalized="https://app.test/login",
        text_digest="a",
        text_simhash=1,
        skeleton_hash="new-skeleton",
        action_sig="new-sig",
        screenshot_dhash=2,
        fingerprint="fp",
    )

    key = key_for(observation)
    assert explorer._identity.find(key) is None
    assert explorer._resolve_existing_state(observation, source, key) == "login"


def test_same_url_substate_skips_url_dedup():
    """SPA sub-states on the same URL still require identity matching."""
    from engine import identity
    from engine.explorer import Explorer, StateMeta
    from engine.identity import key_for
    from engine.schemas import Observation, PageSnapshot, RunConfig, StateType

    explorer = Explorer(Settings(), RunConfig())
    explorer._url_to_state = {}
    explorer._identity = identity.IdentityIndex()
    source = StateMeta(
        id="root",
        index=0,
        url="https://app.test/",
        url_normalized="https://app.test/",
        depth=0,
        path=[],
        state_type=StateType.PAGE,
    )
    explorer._url_to_state["https://app.test/"] = "root"

    observation = Observation(
        snapshot=PageSnapshot(
            url="https://app.test/",
            title="Tab panel",
            visible_text="integrations",
            html="<div></div>",
            screenshot_png=b"",
            dom_skeleton="tab-panel",
        ),
        interactables=[],
        url_normalized="https://app.test/",
        text_digest="b",
        text_simhash=3,
        skeleton_hash="tab-skel",
        action_sig="tab-sig",
        screenshot_dhash=4,
        fingerprint="fp2",
    )
    key = key_for(observation)
    assert explorer._resolve_existing_state(observation, source, key) is None
