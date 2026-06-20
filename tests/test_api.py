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

    # Every state_discovered payload identifies a node.
    node_events = [e["payload"] for e in events if e["type"] == "state_discovered"]
    assert all("state_id" in payload for payload in node_events)

    # The stream agrees with the persisted graph.
    graph = (await client.get(f"/api/runs/{run_id}/graph")).json()
    edge_events = [e for e in kinds if e == "edge_discovered"]
    assert len(edge_events) == len(graph["edges"])


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
