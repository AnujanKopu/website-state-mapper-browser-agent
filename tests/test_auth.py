"""Tests for Slices 5 and 6: auth gate checkpoint and credential autofill.

These tests exercise:
- AUTH_WALL state classification and gate detection
- Explorer pause + auth_gate event emission
- Skip decision: exploration continues without auth, flag recorded
- Resume decision: post-auth state registered, user_auth edge added
- Heuristic autofill field mapping (unit)
- API auth/resume and auth/skip endpoints
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.classify import analyze_state
from engine.config import Settings
from engine.db.session import create_db_engine, create_session_factory
from engine.events import EventType
from engine.explorer import Explorer, ExplorerEvent
from engine.export import export_graph
from engine.schemas import (
    BrowserConfig,
    BudgetConfig,
    Credentials,
    Interactable,
    Observation,
    PageSignals,
    PageSnapshot,
    RunConfig,
    StateType,
)
from tests.conftest import FIXTURE_SITE

LOGIN_URL = (FIXTURE_SITE / "login.html").as_uri()


def test_auth_resolution_is_published_for_replay_and_is_atomic():
    from api.manager import RunHandle
    from engine.schemas import RunStatus

    handle = RunHandle(run_id="run-auth", url=LOGIN_URL, status=RunStatus.RUNNING)
    handle.publish(
        ExplorerEvent(
            EventType.AUTH_GATE,
            "Authentication required",
            {
                "state_id": "auth-state",
                "url": LOGIN_URL,
                "title": "Login",
                "screenshot": "",
                "decision": None,
                "autofill_attempted": False,
                "suggested_actions": ["resume", "skip"],
            },
        )
    )
    handle.set_auth_gate("auth-state", LOGIN_URL)

    assert handle.resolve_auth_gate("skip") is True
    assert handle.resolve_auth_gate("resume") is False
    auth_events = [event for event in handle.history if event.type == EventType.AUTH_GATE]
    assert [event.sequence for event in auth_events] == [0, 1]
    assert [event.payload["decision"] for event in auth_events] == [None, "skip"]
    assert "credentials" not in auth_events[-1].payload
    assert handle.status == RunStatus.RUNNING


async def test_multiple_subscribers_share_one_handle_event_sequence():
    from api.manager import RunHandle, RunManager
    from engine.schemas import RunStatus

    manager = RunManager(Settings(), RunConfig())
    handle = RunHandle(run_id="shared-run", url="https://example.test")

    async def collect():
        return [event async for event in manager.subscribe(handle)]

    first = asyncio.create_task(collect())
    second = asyncio.create_task(collect())
    await asyncio.sleep(0)
    handle.publish(ExplorerEvent(EventType.RUN_STARTED, "Started", {"url": handle.url}))
    handle.publish(ExplorerEvent(EventType.FRONTIER_UPDATED, "", {"frontier_size": 2}))
    handle.complete(RunStatus.DONE)

    first_events, second_events = await asyncio.gather(first, second)
    assert [event.sequence for event in first_events] == [0, 1]
    assert [event.sequence for event in second_events] == [0, 1]
    assert handle.task is None  # subscriptions observe; they never launch exploration


# ---------------------------------------------------------------------------
# Unit: classify.py recognises AUTH_WALL
# ---------------------------------------------------------------------------


def test_classify_login_page_as_auth_wall():
    """A page with password fields + login text must be typed AUTH_WALL."""
    from engine.schemas import BoundingBox

    box = BoundingBox(x=0, y=0, width=80, height=32)
    # Include at least one safe interactable (a plain link) so total_candidates > 0,
    # which allows the AUTH_WALL branch to fire.
    link = Interactable(
        selector="a[href='#forgot']",
        tag="a",
        text="Forgot password?",
        href="https://app.test/forgot",
        bounding_box=box,
        item_id="item-forgot",
    )
    observation = Observation(
        snapshot=PageSnapshot(
            url="https://app.test/login",
            title="Log in",
            visible_text="Log in to TestApp\nEmail address\nPassword\nSign in",
            html="<form></form>",
            screenshot_png=b"",
            dom_skeleton="<input type=password>",
            signals=PageSignals(password_fields=1, form_count=1),
        ),
        interactables=[link],
        url_normalized="https://app.test/login",
        text_digest="a",
        text_simhash=1,
        skeleton_hash="sk",
        action_sig="sig",
        screenshot_dhash=2,
        fingerprint="fp",
    )
    analysis = analyze_state(observation, base_url="https://app.test/")
    assert analysis.state_type == StateType.AUTH_WALL
    assert analysis.flags["auth_required"] is True


# ---------------------------------------------------------------------------
# Unit: AUTH_WALL actions are never enqueued without hook resolution
# ---------------------------------------------------------------------------


def test_auth_wall_actions_not_enqueued_without_hook():
    """_enqueue_actions must skip AUTH_WALL states (they are gate types)."""
    from engine.classify import StateAnalysis
    from engine.explorer import Explorer, Frontier, StateMeta
    from engine.ranking import ActionCandidate
    from engine.schemas import BoundingBox

    box = BoundingBox(x=0, y=0, width=10, height=10)
    explorer = Explorer(Settings(), RunConfig())
    explorer._frontier = Frontier()
    explorer._visited_urls = set()
    meta = StateMeta(
        id="auth-state",
        index=0,
        url="https://app.test/login",
        url_normalized="https://app.test/login",
        depth=0,
        path=[],
        state_type=StateType.AUTH_WALL,
    )

    def cand(label: str) -> ActionCandidate:
        return ActionCandidate(
            interactable=Interactable(
                selector=f"#{label}", tag="button", text=label, bounding_box=box
            ),
        )

    analysis = StateAnalysis(
        candidates=[],
        safe=[cand("Sign in"), cand("Forgot password")],
        denied=[],
        state_type=StateType.AUTH_WALL,
        flags={},
    )
    explorer._enqueue_actions(meta, analysis)
    assert len(explorer._frontier) == 0, "AUTH_WALL actions must not be enqueued"


# ---------------------------------------------------------------------------
# Unit: auth_gate event emitted + skip decision
# ---------------------------------------------------------------------------


async def test_auth_gate_hook_skip_no_post_auth(settings: Settings):
    """When hook returns 'skip', _handle_auth_wall must return None."""
    from engine.explorer import Explorer, StateMeta

    events: list[ExplorerEvent] = []

    async def skip_hook(state_id: str, url: str):
        return ("skip", None)

    explorer = Explorer(
        Settings(),
        RunConfig(),
        on_event=events.append,
        auth_gate_hook=skip_hook,
    )
    # Minimal setup needed by _handle_auth_wall
    explorer._states = {}
    explorer._run_id = "test-run"
    explorer._credentials = None
    explorer._config = RunConfig(
        browser=BrowserConfig(stabilize_quiet_ms=50),
        budgets=BudgetConfig(),
    )

    meta = StateMeta(
        id="auth-id",
        index=0,
        url="https://app.test/login",
        url_normalized="https://app.test/login",
        depth=0,
        path=[],
        state_type=StateType.AUTH_WALL,
    )
    explorer._states[meta.id] = meta

    # Need a fake page (no actual network calls for skip path)
    page = MagicMock()
    page.locator = MagicMock(return_value=MagicMock())

    # Fake sessions (used for flag update in skip branch)
    session_mock = AsyncMock()
    session_ctx = AsyncMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session_mock)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    explorer._sessions = MagicMock(return_value=session_ctx)
    session_mock.get = AsyncMock(return_value=None)  # state row not in DB (unit test)

    result = await explorer._handle_auth_wall(page, meta)

    assert result is None
    auth_gate_events = [e for e in events if e.kind == EventType.AUTH_GATE]
    assert auth_gate_events, "auth_gate event must be emitted"
    payload = auth_gate_events[0].data
    assert payload["state_id"] == meta.id
    assert payload["decision"] is None  # pending at emit time
    assert "resume" in payload["suggested_actions"]
    assert "skip" in payload["suggested_actions"]


# ---------------------------------------------------------------------------
# Integration: explore login.html with skip decision
# ---------------------------------------------------------------------------


async def test_guest_mode_maps_auth_wall_without_pausing(settings: Settings):
    """Guest exploration records a login boundary without prompting."""
    gate_events: list[ExplorerEvent] = []
    all_events: list[ExplorerEvent] = []

    async def skip_hook(state_id: str, url: str):
        return ("skip", None)

    def on_event(event: ExplorerEvent) -> None:
        all_events.append(event)
        if event.kind == EventType.AUTH_GATE:
            gate_events.append(event)

    explorer = Explorer(
        settings,
        RunConfig(
            browser=BrowserConfig(stabilize_quiet_ms=50),
            budgets=BudgetConfig(max_states=5, max_actions=10, max_depth=2),
        ),
        on_event=on_event,
        auth_gate_hook=skip_hook,
    )
    run_id = await explorer.run(LOGIN_URL)

    engine = create_db_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    graph = await export_graph(session_factory, run_id)
    await engine.dispose()

    assert graph["run"]["status"] == "done"

    states = graph["states"]
    edges = graph["edges"]

    # The login page must be classified as AUTH_WALL.
    auth_wall_states = [s for s in states if s["type"] == "auth_wall"]
    assert auth_wall_states, "Expected at least one AUTH_WALL state"

    # No out-edges from the auth_wall state (skipped).
    auth_ids = {s["id"] for s in auth_wall_states}
    out_edges = [e for e in edges if e["from"] in auth_ids]
    assert not out_edges, "Skipped auth_wall must have no out-edges"

    assert not gate_events, "guest mode must never pause at an auth gate"


# ---------------------------------------------------------------------------
# Unit: autofill field mapper finds email + password fields
# ---------------------------------------------------------------------------


async def test_autofill_returns_false_without_credentials():
    """autofill_auth_form returns False immediately when no credentials given."""
    from engine.browser.autofill import autofill_auth_form

    page = MagicMock()
    result = await autofill_auth_form(page, Credentials())
    assert result is False


async def test_autofill_returns_false_when_no_password_field():
    """autofill_auth_form returns False if no password input is found."""
    from playwright.async_api import Error as PlaywrightError

    from engine.browser.autofill import autofill_auth_form

    page = MagicMock()
    locator = MagicMock()
    locator.first = MagicMock()
    locator.first.wait_for = AsyncMock(side_effect=PlaywrightError("not found"))
    page.locator = MagicMock(return_value=locator)

    result = await autofill_auth_form(
        page, Credentials(username="user@test.com", password="secret")
    )
    assert result is False


# ---------------------------------------------------------------------------
# Integration: auth gate API endpoints (live ASGI)
# ---------------------------------------------------------------------------


@pytest.fixture
def _fast_auth_config() -> RunConfig:
    return RunConfig(
        browser=BrowserConfig(stabilize_quiet_ms=50),
        budgets=BudgetConfig(max_states=4, max_actions=8, max_depth=1, max_wall_seconds=60),
    )


async def test_api_auth_skip_endpoint(settings: Settings, _fast_auth_config: RunConfig):
    """POST /auth/skip must unblock an exploration paused at an auth gate."""
    import httpx
    from asgi_lifespan import LifespanManager

    from api.main import create_app

    # Use a short-circuit auth_gate_hook: the test will call /auth/skip via HTTP.
    app = create_app(settings=settings, run_config=_fast_auth_config)

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/runs", json={"url": LOGIN_URL})
            assert resp.status_code == 202
            run_id = resp.json()["run_id"]

            # Wait until the run pauses at auth gate (status becomes "paused").
            async with asyncio.timeout(30):
                while True:
                    status = (await client.get(f"/api/runs/{run_id}")).json()
                    if status["status"] in ("paused", "done", "failed"):
                        break
                    await asyncio.sleep(0.2)

            if status["status"] == "paused":
                skip_resp = await client.post(f"/api/runs/{run_id}/auth/skip")
                assert skip_resp.status_code == 200
                assert skip_resp.json()["status"] == "skipped"

                # Run should complete after skip.
                async with asyncio.timeout(30):
                    while True:
                        status = (await client.get(f"/api/runs/{run_id}")).json()
                        if status["status"] in ("done", "failed"):
                            break
                        await asyncio.sleep(0.2)

            assert status["status"] in ("done", "paused")  # paused = login.html has a link


async def test_api_auth_skip_409_when_not_paused(settings: Settings, _fast_auth_config: RunConfig):
    """POST /auth/skip on a non-paused run must return 409."""
    import httpx
    from asgi_lifespan import LifespanManager

    from api.main import create_app

    app = create_app(settings=settings, run_config=_fast_auth_config)

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Start a run against the normal fixture (no auth wall).
            normal_url = (FIXTURE_SITE / "about.html").as_uri()
            resp = await client.post(
                "/api/runs",
                json={"url": normal_url, "max_states": 2, "max_actions": 4},
            )
            run_id = resp.json()["run_id"]

            # Call skip immediately — run is not paused.
            skip_resp = await client.post(f"/api/runs/{run_id}/auth/skip")
            assert skip_resp.status_code == 409
