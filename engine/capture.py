"""State capture: observe a page, then (optionally) persist it as a state.

Split into two phases so the explorer can deduplicate cheaply:

- `observe_page`  -- snapshot + interactables + identity signals; no side
  effects, safe to throw away when the observation matches a known state.
- `persist_state` -- write the screenshot/DOM artifacts for an observation
  that is being kept as a new state.

`run_single_capture` wraps both in a complete run lifecycle for the
`capture` CLI command.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from playwright.async_api import Page

from engine import identity, storage
from engine.browser.actions import discover_interactables
from engine.browser.session import BrowserSession
from engine.browser.snapshot import stabilize, take_snapshot
from engine.classify import analyze_state
from engine.config import Settings, load_run_config
from engine.db import models as db
from engine.db.session import create_db_engine, create_session_factory, init_db
from engine.schemas import ActionStep, CapturedState, Observation, RunConfig
from engine.storage import LocalStorage, StorageBackend


def new_id() -> str:
    return uuid.uuid4().hex


async def observe_page(page: Page, config: RunConfig) -> Observation:
    """Stabilize and observe the current page; compute all identity signals."""
    await stabilize(page, config.browser.stabilize_quiet_ms)
    snapshot = await take_snapshot(page, config.capture)
    interactables = await discover_interactables(page, config.capture.max_interactables)

    url_normalized = identity.normalize_url(snapshot.url)
    skeleton_hash = identity.dom_skeleton_hash(snapshot.dom_skeleton)
    action_sig = identity.action_signature(interactables)
    fingerprint = identity.state_fingerprint(
        url_normalized,
        "modal" if snapshot.signals.modal_open else "page",
        skeleton_hash,
        action_sig,
    )

    return Observation(
        snapshot=snapshot,
        interactables=interactables,
        url_normalized=url_normalized,
        text_digest=identity.text_hash(snapshot.visible_text),
        text_simhash=identity.text_simhash(snapshot.visible_text),
        skeleton_hash=skeleton_hash,
        action_sig=action_sig,
        screenshot_dhash=identity.screenshot_dhash(snapshot.screenshot_png),
        fingerprint=fingerprint,
    )


def persist_state(
    observation: Observation,
    *,
    run_id: str,
    state_id: str,
    depth: int,
    path: list[ActionStep],
    store: StorageBackend,
) -> CapturedState:
    """Write artifacts for an observation that is being kept as a new state."""
    snapshot = observation.snapshot
    screenshot_path = store.save_bytes(
        storage.screenshot_key(run_id, state_id), snapshot.screenshot_png
    )
    dom_snapshot_path = store.save_text(storage.dom_snapshot_key(run_id, state_id), snapshot.html)

    return CapturedState(
        state_id=state_id,
        run_id=run_id,
        url=snapshot.url,
        url_normalized=observation.url_normalized,
        title=snapshot.title,
        fingerprint=observation.fingerprint,
        text_hash=observation.text_digest,
        text_simhash=observation.text_simhash,
        skeleton_hash=observation.skeleton_hash,
        action_sig=observation.action_sig,
        screenshot_dhash=observation.screenshot_dhash,
        signals=snapshot.signals,
        visible_text=snapshot.visible_text,
        interactables=observation.interactables,
        screenshot_path=screenshot_path,
        dom_snapshot_path=dom_snapshot_path,
        path=path,
        depth=depth,
    )


def build_state_row(state: CapturedState) -> db.StateNode:
    """Map a CapturedState onto its database row."""
    return db.StateNode(
        id=state.state_id,
        run_id=state.run_id,
        fingerprint=state.fingerprint,
        url=state.url,
        url_normalized=state.url_normalized,
        title=state.title,
        state_type=state.state_type.value,
        screenshot_path=state.screenshot_path,
        dom_snapshot_path=state.dom_snapshot_path,
        text_hash=state.text_hash,
        dom_skeleton_hash=state.skeleton_hash,
        text_simhash=f"{state.text_simhash:016x}",
        screenshot_dhash=f"{state.screenshot_dhash:016x}",
        interactables=[i.model_dump() for i in state.interactables],
        detected_flags=state.detected_flags,
        path=[step.model_dump() for step in state.path],
        depth=state.depth,
    )


async def run_single_capture(
    url: str,
    settings: Settings,
    *,
    headless: bool | None = None,
) -> CapturedState:
    """Capture a single URL end to end: run row, browser, artifacts, state row."""
    config = load_run_config(settings.run_config_path)
    if headless is not None:
        config.browser.headless = headless

    engine = create_db_engine(settings.database_url)
    await init_db(engine)
    session_factory = create_session_factory(engine)
    store = LocalStorage(settings.data_dir)

    run_id = new_id()
    async with session_factory() as session:
        session.add(db.Run(id=run_id, url=url, status="running", config=config.model_dump()))
        await session.commit()

    try:
        async with BrowserSession(config.browser) as browser:
            page = await browser.new_page()
            await page.goto(url)
            observation = await observe_page(page, config)
    except Exception as exc:
        await _finish_run(session_factory, run_id, status="failed", error=str(exc))
        await engine.dispose()
        raise

    state = persist_state(
        observation,
        run_id=run_id,
        state_id=new_id(),
        depth=0,
        path=[ActionStep(kind="goto", url=url)],
        store=store,
    )
    analysis = analyze_state(observation, base_url=url)
    state.state_type = analysis.state_type
    state.detected_flags = analysis.flags

    async with session_factory() as session:
        session.add(build_state_row(state))
        await session.commit()

    await _finish_run(
        session_factory,
        run_id,
        status="done",
        stats={
            "states": 1,
            "interactables": len(state.interactables),
            "visible_text_chars": len(state.visible_text),
        },
    )
    await engine.dispose()
    return state


async def _finish_run(
    session_factory,
    run_id: str,
    *,
    status: str,
    stats: dict | None = None,
    error: str | None = None,
) -> None:
    async with session_factory() as session:
        run = await session.get(db.Run, run_id)
        run.status = status
        run.finished_at = datetime.now(UTC)
        if stats is not None:
            run.stats = stats
        if error is not None:
            run.error = error
        await session.commit()
