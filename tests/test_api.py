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


async def _read_sse(client: httpx.AsyncClient, url: str, stop_kind: str) -> list[tuple[str, dict]]:
    """Collect (event_kind, payload) pairs until `stop_kind` arrives."""
    events: list[tuple[str, dict]] = []
    kind: str | None = None
    async with asyncio.timeout(120):
        async with client.stream("GET", url) as response:
            assert response.status_code == 200
            async for raw in response.aiter_lines():
                line = raw.strip()
                if line.startswith("event:"):
                    kind = line[len("event:") :].strip()
                elif line.startswith("data:") and kind:
                    events.append((kind, json.loads(line[len("data:") :].strip())))
                    if kind == stop_kind:
                        return events
                    kind = None
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


async def test_sse_streams_node_and_edge_events(client: httpx.AsyncClient):
    run_id = (await client.post("/api/runs", json={"url": FIXTURE_URL})).json()["run_id"]

    events = await _read_sse(client, f"/api/runs/{run_id}/events", stop_kind="run_finished")
    kinds = [kind for kind, _ in events]

    assert "run_started" in kinds
    assert "state_new" in kinds  # node events
    assert "edge_created" in kinds  # edge events
    assert kinds[-1] == "run_finished"

    # Every state_new payload identifies a node; sequence ids are monotonic.
    node_events = [payload for kind, payload in events if kind == "state_new"]
    assert all("state_id" in payload["data"] for payload in node_events)

    # The stream agrees with the persisted graph.
    graph = (await client.get(f"/api/runs/{run_id}/graph")).json()
    edge_events = [k for k in kinds if k == "edge_created"]
    assert len(edge_events) == len(graph["edges"])


async def test_unknown_run_is_404(client: httpx.AsyncClient):
    assert (await client.get("/api/runs/does-not-exist")).status_code == 404
    assert (await client.get("/api/runs/does-not-exist/graph")).status_code == 404
    assert (await client.get("/api/runs/does-not-exist/events")).status_code == 404
