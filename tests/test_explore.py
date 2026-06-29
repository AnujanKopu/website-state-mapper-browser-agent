"""End-to-end exploration test against the fixture site.

Drives the full engine -- frontier loop, ranking, safety, dedup, sibling
collapse, budgets, export -- with real Chromium over file:// URLs and
asserts on the resulting graph structure.
"""

import json
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

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


async def test_broad_navigation_and_local_controls_use_separate_layers(
    settings: Settings, tmp_path: Path
):
    explorer = Explorer(settings, _test_config())
    run_id = await explorer.run((FIXTURE_SITE / "controls.html").as_uri())
    engine = create_db_engine(settings.database_url)
    sessions = create_session_factory(engine)
    graph = await export_graph(sessions, run_id)
    await engine.dispose()

    root = next(state for state in graph["states"] if state["depth"] == 0)
    urls = {state["url_normalized"] for state in graph["states"]}
    for filename in (
        "login.html",
        "signup.html",
        "pricing.html",
        "docs.html",
        "post1.html",
        "spa.html",
    ):
        assert any(url.endswith(filename) for url in urls), filename
    signup = next(
        state for state in graph["states"] if state["url_normalized"].endswith("signup.html")
    )
    assert signup["type"] == "form"
    assert signup["flags"]["auth_surface_kind"] == "registration"

    by_label = {item["label"].lower(): item for item in root["surface_items"]}
    for label in ("search", "all categories", "chart", "metrics (7/34)", "table"):
        assert label in by_label
    assert by_label["download"]["status"] == "blocked"
    assert by_label["download"]["execution_policy"] == "blocked"
    assert not list(tmp_path.rglob("games.csv"))
    assert any(
        item["label"].lower() == "search" and item["probe_reason"] == "focus_search"
        for item in root["surface_items"]
    )
    assert graph["run"]["stats"]["local_probes"] > 0

    nested = [
        state for state in graph["states"] if state["parent_state_id"] == root["id"]
    ]
    assert nested
    assert any(state["type"] in {"modal", "dropdown"} for state in nested)
    assert any("?filter=" in state["url"] for state in nested)


async def test_navigation_disclosure_reveals_primary_routes_once(settings: Settings):
    config = RunConfig(
        browser=BrowserConfig(stabilize_quiet_ms=50),
        budgets=BudgetConfig(
            max_states=30,
            max_actions=40,
            max_depth=1,
            max_wall_seconds=120,
        ),
    )
    explorer = Explorer(settings, config)
    run_id = await explorer.run((FIXTURE_SITE / "navigation.html").as_uri())
    engine = create_db_engine(settings.database_url)
    sessions = create_session_factory(engine)
    graph = await export_graph(sessions, run_id)
    await engine.dispose()

    root = next(state for state in graph["states"] if state["depth"] == 0)
    by_label = {item["label"]: item for item in root["surface_items"]}
    assert {"Music", "Movies & TV", "Live", "Gaming", "News", "Learning"} <= set(
        by_label
    )
    assert by_label["Guide"]["status"] == "inventory_only"
    assert by_label["Show more"]["status"] == "explored"
    stats = graph["run"]["stats"]
    assert stats["navigation_links_revealed"] == 3
    assert stats["stale_actions"] == 0
    assert stats["unresolved_discovery_obligations"] == 0


async def test_required_navigation_failure_prevents_false_completion(settings: Settings):
    config = RunConfig(
        browser=BrowserConfig(stabilize_quiet_ms=50),
        budgets=BudgetConfig(max_states=10, max_actions=10, max_depth=1, max_wall_seconds=60),
    )
    explorer = Explorer(settings, config)
    run_id = await explorer.run((FIXTURE_SITE / "required-failure.html").as_uri())
    engine = create_db_engine(settings.database_url)
    sessions = create_session_factory(engine)
    graph = await export_graph(sessions, run_id)
    await engine.dispose()

    stats = graph["run"]["stats"]
    assert stats["completion_status"] == "partial"
    assert stats["failed_actions"] == 1
    assert stats["required_action_failures"] == 1


async def test_semantic_edges_survive_legacy_selector_uniqueness(settings: Settings):
    from sqlalchemy import select

    from engine.db import models as db
    from engine.db.session import init_db
    from engine.explorer import Frontier, StateMeta
    from engine.schemas import StateType

    engine = create_db_engine(settings.database_url)
    await init_db(engine)
    sessions = create_session_factory(engine)
    run_id = "selector-collision"

    def row(state_id: str, fingerprint: str) -> db.StateNode:
        return db.StateNode(
            id=state_id,
            run_id=run_id,
            fingerprint=fingerprint,
            url=f"https://app.test/{state_id}",
            url_normalized=f"https://app.test/{state_id}",
            screenshot_path="",
            dom_snapshot_path="",
            text_hash=fingerprint,
        )

    async with sessions() as session:
        session.add(db.Run(id=run_id, url="https://app.test", status="running"))
        session.add_all([row("source", "source-fp"), row("one", "one-fp"), row("two", "two-fp")])
        await session.commit()

    explorer = Explorer(settings, RunConfig(), session_factory=sessions)
    explorer._run_id = run_id
    explorer._sessions = sessions
    explorer._edge_records = {}
    explorer._stats = defaultdict(int)
    explorer._timings = defaultdict(float)
    explorer._budget = SimpleNamespace(actions=0)
    explorer._frontier = Frontier()
    source = StateMeta(
        id="source", index=0, url="https://app.test/source",
        url_normalized="https://app.test/source", depth=0, path=[], state_type=StateType.PAGE,
    )
    destinations = [
        StateMeta(
            id=state_id, index=index, url=f"https://app.test/{state_id}",
            url_normalized=f"https://app.test/{state_id}", depth=1, path=[],
            state_type=StateType.PAGE,
        )
        for index, state_id in enumerate(("one", "two"), 1)
    ]
    common = dict(
        action_type="click",
        label="Activate control",
        selector="body > div > a",
        element_text="Control",
        confidence=1.0,
        collapsed_count=1,
        via="performed",
        surface_item_id="control",
        transition_kind="link",
        scope="local",
        reversible=False,
        evidence={"mode": "performed", "validated": True},
    )

    await explorer._upsert_transition(source, destinations[0], capability_id="first", **common)
    await explorer._upsert_transition(source, destinations[1], capability_id="second", **common)

    async with sessions() as session:
        edges = (
            await session.execute(select(db.Edge).where(db.Edge.run_id == run_id))
        ).scalars().all()
    await engine.dispose()

    assert len(edges) == 2
    assert len({edge.transition_key for edge in edges}) == 2
    assert len({edge.selector for edge in edges}) == 2
    assert any("/*flowstate-edge:" in edge.selector for edge in edges)


def test_active_budget_ignores_auth_wait_and_novelty_streaks():
    from engine.explorer import Budget

    budget = Budget(BudgetConfig(max_wall_seconds=10))
    budget._started -= 100
    budget._paused_total = 95

    assert budget.active_elapsed_seconds < 10
    assert budget.stop_reason(state_count=1, actions_since_new=10_000) is None


async def test_exploration_builds_state_graph(settings: Settings, tmp_path: Path):
    graph = await _run_exploration(settings)
    states, edges = graph["states"], graph["edges"]
    by_id = {s["id"]: s for s in states}

    # --- run completed within budget ---
    assert graph["run"]["status"] == "done"
    stats = graph["run"]["stats"]
    assert stats["states"] == len(states)
    assert stats["page_states"] == len([state for state in states if not state["parent_state_id"]])
    assert stats["substates"] >= 1
    assert stats["interaction_nodes"] > 0
    assert stats["edges"] == len(edges)
    assert stats["actions_denied"] > 0
    assert stats["completion_status"] == "complete"

    # --- graph integrity ---
    assert all(e["from"] in by_id and e["to"] in by_id for e in edges)
    root = next(s for s in states if s["depth"] == 0)
    assert root["type"] == "page"

    # --- URL states discovered ---
    urls = {s["url_normalized"] for s in states}
    assert any(u.endswith("pricing.html") for u in urls)
    assert any(u.endswith("docs.html") for u in urls)
    assert any(u.endswith("checkout.html") for u in urls)

    # --- structural local controls are probed into the nested layer ---
    assert any(s["parent_state_id"] == root["id"] for s in states)
    root_items = root["surface_items"]
    assert any(item["kind"] == "tab" for item in root_items)
    assert any(item["label"] == "Sign up" for item in root_items)
    local_items = [item for item in root_items if item["interaction_scope"] == "local_ui"]
    assert local_items
    assert any(item["status"] == "explored" for item in local_items)
    assert all(
        item["status"] in {"explored", "noop", "inventory_only", "blocked", "skipped_duplicate"}
        for item in local_items
    )

    # --- payment-like terminal detected and not expanded ---
    payment_boundaries = [s for s in states if s["flags"].get("payment_required")]
    assert len(payment_boundaries) == 1
    checkout = payment_boundaries[0]
    assert checkout["url_normalized"].endswith("checkout.html")
    assert checkout["flags"]["payment_required"] is True
    denied_categories = {d["category"] for d in checkout["flags"]["denied_actions"]}
    assert "payment" in denied_categories
    checkout_out = [e for e in edges if e["from"] == checkout["id"]]
    # Direct safe URL navigation does not synthesize browser Back/Forward
    # observations, and a payment boundary is never expanded.
    assert checkout_out == []

    # --- peripheral resources are recorded as skipped surface items instead
    # of consuming the main product-state budget.
    skipped_about = [
        item
        for state in states
        for item in state["surface_items"]
        if item["label"] == "About the project"
    ]
    assert skipped_about
    assert any(item["status"] == "skipped_duplicate" for item in skipped_about)

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

    # --- cyclic state-machine semantics remain, but V1 avoids materializing a
    # full shared-navbar clique as primary graph structure.
    home = root
    pricing = next(s for s in states if s["url_normalized"].endswith("pricing.html"))
    docs = next(s for s in states if s["url_normalized"].endswith("docs.html"))
    pairs = {(e["from"], e["to"]) for e in edges}
    assert (home["id"], pricing["id"]) in pairs
    assert (home["id"], docs["id"]) in pairs
    global_pairs = {(e["from"], e["to"]) for e in edges if e["scope"] == "global_navigation"}
    full_nav_clique = {
        (source["id"], destination["id"])
        for source in (home, pricing, docs)
        for destination in (home, pricing, docs)
        if source["id"] != destination["id"]
    }
    assert global_pairs != full_nav_clique

    # The home -> Docs control is represented once as a performed transition;
    # reusable navbar capability evidence lives in state metadata instead of
    # being expanded into inferred all-pairs topology.
    home_docs = [
        e
        for e in edges
        if e["from"] == home["id"]
        and e["to"] == docs["id"]
        and e["surface_item_id"]
        and "Docs" in e["label"]
    ]
    assert len(home_docs) == 1
    assert "performed" in set(home_docs[0]["provenance"])
    nav_capabilities = root["exploration"].get("nav_capabilities", [])
    assert any(
        item["label"] == "Docs" and item["target_state_id"] == docs["id"]
        for item in nav_capabilities
    )

    # Browser back edges exist only as validated restoration evidence.
    history_edges = [e for e in edges if e["transition_kind"] == "back"]
    assert history_edges
    assert all(e["reversible"] for e in history_edges)
    assert all(
        any(
            ev.get("mechanism") != "browser_history" or ev.get("validated") is True
            for ev in e["evidence"]
        )
        for e in history_edges
    )

    # Tabs are local transitions and their states stay out of page topology.
    assert any(e["transition_kind"] == "tab" for e in edges)

    # Stable semantic keys prevent duplicate inferred/performed rows.
    assert len({e["transition_key"] for e in edges}) == len(edges)

    # --- surface items + coverage are exported per state ---
    assert root["surface_items"], "root should expose surface items"
    assert all("status" in item and "item_id" in item for item in root["surface_items"])
    assert root["exploration"].get("visit_status") == "fully_explored"
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
    # All three exact representatives remain persisted. Projection may later
    # collapse equivalent samples to one visible structural variant.
    assert len(profile_states) == 3
    assert max(
        state["exploration"].get("family", {}).get("checked_count", 0)
        for state in profile_states
    ) == 3
    assert any(state["url"].endswith("alice.html") for state in profile_states)
    assert any(state["url"].endswith("bob.html") for state in profile_states)
    assert any(state["url"].endswith("moderator.html") for state in profile_states)
    assert not any(state["url"].endswith("carol.html") for state in profile_states)

    root = next(state for state in graph["states"] if state["depth"] == 0)
    family_items = [
        item for item in root["surface_items"] if item.get("href") and "/profiles/" in item["href"]
    ]
    assert len(family_items) == 4
    assert len({item["group_id"] for item in family_items}) == 1
    assert any(
        item["href"].endswith("carol.html") and item["status"] == "skipped_duplicate"
        for item in family_items
    )

    stats = graph["run"]["stats"]
    # All three required samples retain exact states; only later, unsampled
    # equivalent members may be suppressed by the confirmed family.
    assert stats["family_dedup_hits"] == 0
    assert stats["family_urls_skipped"] == 1
    assert stats["actions_performed"] <= 5

    family_meta = [state["exploration"] for state in profile_states]
    assert all(meta["route_family"] for meta in family_meta)
    assert all(meta["family_sampled"] == 3 for meta in family_meta)
    assert all(meta["family_skipped"] == 1 for meta in family_meta)
    assert all(len(meta["family"]["sample_state_ids"]) == 3 for meta in family_meta)
    assert any(edge["collapsed_count"] == 4 for edge in graph["edges"])


def test_rejected_family_releases_deferred_actions():
    """A false family must return every held action to the normal frontier."""
    from engine.explorer import Frontier, PendingAction, StateMeta
    from engine.families import FamilyRegistry
    from engine.ranking import ActionCandidate
    from engine.schemas import BoundingBox, Interactable, StateType

    box = BoundingBox(x=0, y=0, width=10, height=10)
    registry = FamilyRegistry()
    items = [
        Interactable(
            selector=f"#mixed > a:nth-of-type({index})",
            tag="a",
            text=value,
            href=f"https://app.test/mixed/{value}",
            bounding_box=box,
            item_id=value,
            region="main",
            container_key="mixed",
        )
        for index, value in enumerate(("one", "two", "three", "four"), 1)
    ]
    family = registry.observe_surface(
        source_key="root",
        source_structure="root-structure",
        base_url="https://app.test/",
        items=items,
    )[0]
    family.status = "rejected"

    explorer = Explorer(Settings(), RunConfig())
    explorer._family_registry = registry
    explorer._family_deferred = {family.pattern: []}
    explorer._frontier = Frontier()
    explorer._family_sampled_urls = {}
    explorer._family_skipped = {}
    explorer._family_info = {}
    explorer._family_variants = {}
    explorer._edges_done = set()
    explorer._item_outcome = {}
    explorer._stats = {}
    source = StateMeta(
        id="root",
        index=0,
        url="https://app.test/",
        url_normalized="https://app.test/",
        depth=0,
        path=[],
        state_type=StateType.PAGE,
    )
    for item in items:
        candidate = ActionCandidate(
            interactable=item,
            family_pattern=family.pattern,
            family_id=family.family_id,
            family_status="provisional",
        )
        explorer._family_deferred[family.pattern].append(
            PendingAction(from_state=source, candidate=candidate)
        )

    released = explorer._release_family_deferred(family, rejected=True)

    assert released == len(items)
    assert len(explorer._frontier) == len(items)
    assert all(explorer._frontier.pop().candidate.family_pattern is None for _ in range(len(items)))
    assert not explorer._item_outcome


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
    explorer._root_url = "https://app.test/"
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
                selector=f"#{label}",
                tag="a",
                text=label,
                href=f"/{label}",
                execution_policy="navigate",
                bounding_box=box,
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


def test_interaction_policy_probes_structural_controls_only():
    from engine.explorer import Explorer
    from engine.schemas import BoundingBox, Interactable, RunConfig

    explorer = Explorer(Settings(), RunConfig())
    explorer._root_url = "https://app.test/"
    box = BoundingBox(x=0, y=0, width=20, height=20)
    button = Interactable(selector="#filter", tag="button", text="Filter", bounding_box=box)
    search = Interactable(
        selector="#search",
        tag="input",
        input_type="search",
        placeholder="Search",
        bounding_box=box,
    )
    link = Interactable(
        selector="#docs",
        tag="a",
        text="Docs",
        href="https://app.test/docs",
        bounding_box=box,
    )
    external = Interactable(
        selector="#outside",
        tag="a",
        text="Outside",
        href="https://outside.test/",
        bounding_box=box,
    )

    for item in (button, search, link, external):
        explorer._annotate_interaction_policy(item)

    assert button.execution_policy == "probe_local"
    assert button.probe_reason == "labelled_structural_button"
    assert search.execution_policy == "probe_local"
    assert search.probe_reason == "focus_search"
    assert link.execution_policy == "navigate"
    assert external.execution_policy == "blocked"


async def test_browser_back_mismatch_does_not_create_reverse_edge(monkeypatch):
    """A history movement is evidence only when it restores the expected node."""
    from engine.explorer import Explorer, StateMeta
    from engine.schemas import AuthContext, RunConfig, StateType

    explorer = Explorer(Settings(), RunConfig())
    explorer._root_url = "https://app.test/"
    explorer._auth_context = AuthContext.GUEST
    explorer._validated_history = set()
    explorer._edge_records = {}
    explorer._stats = {"restoration_checks": 0}
    explorer._current_state_id = "destination"
    source = StateMeta(
        id="source",
        index=0,
        url="https://app.test/source",
        url_normalized="https://app.test/source",
        depth=0,
        path=[],
        state_type=StateType.PAGE,
    )
    destination = StateMeta(
        id="destination",
        index=1,
        url="https://app.test/destination",
        url_normalized="https://app.test/destination",
        depth=1,
        path=[],
        state_type=StateType.PAGE,
    )

    class FakePage:
        async def go_back(self, **_kwargs):
            return None

        async def go_forward(self, **_kwargs):
            return None

    async def fake_observe(*_args, **_kwargs):
        return object()

    async def fake_stabilize(*_args, **_kwargs):
        return None

    async def fake_ensure(*_args, **_kwargs):
        return True

    monkeypatch.setattr("engine.explorer.observe_page", fake_observe)
    monkeypatch.setattr("engine.explorer.stabilize", fake_stabilize)
    monkeypatch.setattr("engine.explorer.identity.key_for", lambda _observation: object())
    monkeypatch.setattr(explorer, "_resolve_existing_state", lambda *_args: "unexpected")
    monkeypatch.setattr(explorer, "_ensure_at", fake_ensure)

    await explorer._validate_browser_back(FakePage(), source, destination)

    assert explorer._stats["restoration_checks"] == 1
    assert explorer._edge_records == {}
