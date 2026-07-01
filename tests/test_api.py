"""API tests: run lifecycle, graph retrieval, export, and SSE streaming.

Drives the real ASGI app (and a real browser via the engine) against the
offline fixture site using httpx's in-process transport.
"""

import asyncio
import json

import httpx
import pytest
from asgi_lifespan import LifespanManager

from api.main import create_app
from api.schemas import CreateRunRequest
from engine.config import Settings
from engine.db import models as db
from engine.db.session import create_db_engine, create_session_factory, init_db
from engine.schemas import BrowserConfig, BudgetConfig, RunConfig
from tests.conftest import FIXTURE_SITE

FIXTURE_URL = (FIXTURE_SITE / "index.html").as_uri()


def _fast_config() -> RunConfig:
    return RunConfig(
        browser=BrowserConfig(stabilize_quiet_ms=50),
        budgets=BudgetConfig(max_states=8, max_actions=14, max_depth=1, max_wall_seconds=120),
    )


@pytest.fixture
async def client(settings: Settings):
    app = create_app(settings=settings, run_config=_fast_config())
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def _wait_for_status(client: httpx.AsyncClient, run_id: str, target: str) -> dict:
    async with asyncio.timeout(120):
        while True:
            body = (await client.get(f"/api/runs/{run_id}")).json()
            if body["status"] in (target, "failed"):
                return body
            await asyncio.sleep(0.25)


async def _read_sse(client: httpx.AsyncClient, url: str, stop_kind: str) -> list[dict]:
    """Collect SSE envelopes until an event of type `stop_kind` arrives."""
    events: list[dict] = []
    async with asyncio.timeout(120):
        async with client.stream("GET", url) as response:
            assert response.status_code == 200
            async for raw in response.aiter_lines():
                line = raw.strip()
                if line.startswith("data:"):
                    envelope = json.loads(line[len("data:") :].strip())
                    events.append(envelope)
                    if envelope["type"] == stop_kind:
                        return events
    return events


async def test_health(client: httpx.AsyncClient):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_screenshot_artifacts_are_immutable_cached(settings: Settings):
    screenshot = settings.data_dir / "runs" / "run-1" / "screenshots" / "state.webp"
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"artifact")
    app = create_app(settings=settings, run_config=_fast_config())
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.get("/artifacts/runs/run-1/screenshots/state.webp")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


async def test_startup_cancels_runs_that_cannot_be_resumed(settings: Settings):
    engine = create_db_engine(settings.database_url)
    await init_db(engine)
    sessions = create_session_factory(engine)
    async with sessions.begin() as session:
        session.add(db.Run(id="orphaned", url="https://example.com", status="running"))
    await engine.dispose()

    app = create_app(settings=settings, run_config=_fast_config())
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.get("/api/runs/orphaned")

    body = response.json()
    assert body["status"] == "cancelled"
    assert body["finished_at"]
    assert "API restart" in body["error"]


async def test_create_run_returns_identifiers(client: httpx.AsyncClient):
    response = await client.post("/api/runs", json={"url": FIXTURE_URL})
    assert response.status_code == 202
    body = response.json()
    assert body["run_id"]
    assert body["status"] == "running"
    assert body["events_url"] == f"/api/runs/{body['run_id']}/events"
    assert body["graph_url"] == f"/api/runs/{body['run_id']}/graph"


def test_bare_host_gets_https_scheme():
    assert CreateRunRequest(url="example.com").url == "https://example.com"
    assert CreateRunRequest(url="file:///tmp/a.html").url == "file:///tmp/a.html"
    assert CreateRunRequest(url="http://x.test").url == "http://x.test"
    with pytest.raises(ValueError):
        CreateRunRequest(url="   ")


def test_credentials_require_explicit_login_mode():
    with pytest.raises(ValueError):
        CreateRunRequest(
            url="https://example.com",
            credentials={"username": "u", "password": "p"},
        )
    request = CreateRunRequest(
        url="https://example.com",
        auth_mode="login",
        credentials={"username": "u", "password": "p"},
    )
    assert request.overrides()["auth_mode"] == "login"
    assert "credentials" not in request.overrides()


async def test_full_lifecycle_graph_and_export(client: httpx.AsyncClient):
    run_id = (await client.post("/api/runs", json={"url": FIXTURE_URL})).json()["run_id"]

    status = await _wait_for_status(client, run_id, "done")
    assert status["status"] == "done"
    assert status["stats"]["states"] >= 2
    assert status["finished_at"]

    graph = (await client.get(f"/api/runs/{run_id}/graph")).json()
    assert graph["sync"]["schema_version"] == 4
    assert graph["sync"]["authoritative"] is True
    assert [state["index"] for state in graph["states"]] == list(range(len(graph["states"])))
    assert all("evidence" in state for state in graph["states"])
    assert graph["run"]["id"] == run_id
    assert len(graph["states"]) == status["stats"]["states"]
    assert len(graph["edges"]) >= 1
    node_ids = {s["id"] for s in graph["states"]}
    assert all(e["from"] in node_ids and e["to"] in node_ids for e in graph["edges"])

    export = await client.get(f"/api/runs/{run_id}/export")
    assert export.status_code == 200
    assert "attachment" in export.headers["content-disposition"]
    assert f"flowstate-{run_id}.json" in export.headers["content-disposition"]
    assert export.json()["run"]["id"] == run_id


async def test_sse_envelope_and_event_contract(client: httpx.AsyncClient):
    run_id = (await client.post("/api/runs", json={"url": FIXTURE_URL})).json()["run_id"]

    events = await _read_sse(client, f"/api/runs/{run_id}/events", stop_kind="run_completed")
    kinds = [e["type"] for e in events]

    assert "run_started" in kinds
    assert "state_discovered" in kinds
    assert "edge_discovered" in kinds
    assert "action_started" in kinds
    assert "action_finished" in kinds
    assert "frontier_updated" in kinds
    assert "surface_items_discovered" in kinds
    assert kinds[-1] == "run_completed"  # terminal event closes the stream

    # Every envelope carries the contract v1 fields.
    for envelope in events:
        assert set(envelope) >= {
            "event_id",
            "run_id",
            "sequence",
            "timestamp",
            "type",
            "payload",
        }
        assert envelope["run_id"] == run_id

    # Sequence numbers are unique and monotonically increasing.
    sequences = [e["sequence"] for e in events]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)

    resumed = await _read_sse(
        client,
        f"/api/runs/{run_id}/events?after_sequence={sequences[-3]}",
        stop_kind="run_completed",
    )
    assert resumed
    assert all(event["sequence"] > sequences[-3] for event in resumed)

    # Every state_discovered payload identifies a node.
    node_events = [e["payload"] for e in events if e["type"] == "state_discovered"]
    assert all("state_id" in payload for payload in node_events)

    # The stream agrees with the persisted graph.
    graph = (await client.get(f"/api/runs/{run_id}/graph")).json()
    persisted_states = {state["id"]: state for state in graph["states"]}
    live_inventory = next(
        item
        for payload in node_events
        for item in payload.get("surface_items", [])
        if item["status"] == "inventory_only"
    )
    live_state = next(
        payload for payload in node_events if live_inventory in payload.get("surface_items", [])
    )
    persisted_inventory = next(
        item
        for item in persisted_states[live_state["state_id"]]["surface_items"]
        if item["item_id"] == live_inventory["item_id"]
    )
    for field in (
        "kind",
        "tag",
        "role",
        "interaction_scope",
        "execution_policy",
        "controlled_surface",
        "page_box",
    ):
        assert live_inventory[field] == persisted_inventory[field]
    edge_events = [e["payload"] for e in events if e["type"] == "edge_discovered"]
    # Stable edge ids allow later observations to upgrade an inferred edge
    # without creating another graph row.  The final event snapshot for every
    # id must converge with authoritative graph hydration.
    latest_edges = {payload["edge_id"]: payload for payload in edge_events}
    persisted_edges = {edge["id"]: edge for edge in graph["edges"]}
    assert set(latest_edges) == set(persisted_edges)
    for edge_id, persisted in persisted_edges.items():
        live = latest_edges[edge_id]
        assert live["from"] == persisted["from"]
        assert live["to"] == persisted["to"]
        assert live["via"] == persisted["via"]
        assert live["provenance"] == persisted["provenance"]

    # Authoritative post-probe item updates converge with terminal hydration.
    latest_surfaces = {
        event["payload"]["state_id"]: event["payload"]
        for event in events
        if event["type"] == "surface_items_discovered"
    }
    for state_id, payload in latest_surfaces.items():
        live_items = {item["item_id"]: item for item in payload["surface_items"]}
        terminal_items = {
            item["item_id"]: item for item in persisted_states[state_id]["surface_items"]
        }
        assert set(live_items) == set(terminal_items)
        for item_id, live in live_items.items():
            terminal_item = terminal_items[item_id]
            for field in (
                "status",
                "execution_policy",
                "component_key",
                "component_label",
                "icon_label",
                "probe_reason",
                "page_box",
            ):
                assert live[field] == terminal_item[field]


async def test_context_pack_endpoint(client: httpx.AsyncClient):
    run_id = (await client.post("/api/runs", json={"url": FIXTURE_URL})).json()["run_id"]
    await _wait_for_status(client, run_id, "done")

    markdown = await client.get(f"/api/runs/{run_id}/context")
    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert f"flowstate-{run_id}-context.md" in markdown.headers["content-disposition"]
    assert "# FlowState context pack" in markdown.text
    assert "## States" in markdown.text

    structured = await client.get(f"/api/runs/{run_id}/context", params={"format": "json"})
    assert structured.status_code == 200
    body = structured.json()
    assert body["run"]["id"] == run_id
    assert body["site_summary"]["state_count"] >= 2
    assert f"flowstate-{run_id}-context.json" in structured.headers["content-disposition"]

    # an invalid format is rejected by the query validator
    assert (await client.get(f"/api/runs/{run_id}/context?format=pdf")).status_code == 422


async def test_list_runs_returns_started_runs(client: httpx.AsyncClient):
    run_id = (await client.post("/api/runs", json={"url": FIXTURE_URL})).json()["run_id"]
    await _wait_for_status(client, run_id, "done")

    runs = (await client.get("/api/runs")).json()
    assert any(r["run_id"] == run_id for r in runs)
    listed = next(r for r in runs if r["run_id"] == run_id)
    assert listed["url"] == FIXTURE_URL
    assert listed["status"] == "done"


async def test_unknown_run_is_404(client: httpx.AsyncClient):
    assert (await client.get("/api/runs/does-not-exist")).status_code == 404
    assert (await client.get("/api/runs/does-not-exist/graph")).status_code == 404
    assert (await client.get("/api/runs/does-not-exist/events")).status_code == 404
