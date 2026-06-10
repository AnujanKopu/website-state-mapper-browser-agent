"""State capture orchestration.

`capture_state` is the reusable unit of observation -- the exploration
loop (M1) will call it after every action. `run_single_capture` wraps it
in a complete run lifecycle (browser, database, storage) for the CLI.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from playwright.async_api import Page

from engine import identity, storage
from engine.browser.actions import discover_interactables
from engine.browser.session import BrowserSession
from engine.browser.snapshot import stabilize, take_snapshot
from engine.config import Settings, load_run_config
from engine.db import models as db
from engine.db.session import create_db_engine, create_session_factory, init_db
from engine.schemas import CapturedState, RunConfig
from engine.storage import LocalStorage, StorageBackend


def _new_id() -> str:
    return uuid.uuid4().hex


async def capture_state(
    page: Page,
    *,
    run_id: str,
    artifact_store: StorageBackend,
    config: RunConfig,
    depth: int = 0,
) -> CapturedState:
    """Observe the current page and persist its artifacts.

    Assumes navigation has already happened; stabilizes, snapshots,
    discovers interactables, computes identity, and stores the screenshot
    and DOM snapshot. Database persistence is the caller's concern.
    """
    await stabilize(page, config.browser.stabilize_quiet_ms)
    snapshot = await take_snapshot(page, config.capture)
    interactables = await discover_interactables(page, config.capture.max_interactables)

    url_normalized = identity.normalize_url(snapshot.url)
    text_digest = identity.text_hash(snapshot.visible_text)
    fingerprint = identity.state_fingerprint(url_normalized, text_digest)

    state_id = _new_id()
    screenshot_path = artifact_store.save_bytes(
        storage.screenshot_key(run_id, state_id), snapshot.screenshot_png
    )
    dom_snapshot_path = artifact_store.save_text(
        storage.dom_snapshot_key(run_id, state_id), snapshot.html
    )

    return CapturedState(
        state_id=state_id,
        run_id=run_id,
        url=snapshot.url,
        url_normalized=url_normalized,
        title=snapshot.title,
        fingerprint=fingerprint,
        text_hash=text_digest,
        visible_text=snapshot.visible_text,
        interactables=interactables,
        screenshot_path=screenshot_path,
        dom_snapshot_path=dom_snapshot_path,
        depth=depth,
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
    artifact_store = LocalStorage(settings.data_dir)

    run_id = _new_id()
    async with session_factory() as session:
        session.add(
            db.Run(id=run_id, url=url, status="running", config=config.model_dump())
        )
        await session.commit()

    try:
        async with BrowserSession(config.browser) as browser:
            page = await browser.new_page()
            await page.goto(url)
            state = await capture_state(
                page, run_id=run_id, artifact_store=artifact_store, config=config
            )
    except Exception as exc:
        await _finish_run(session_factory, run_id, status="failed", error=str(exc))
        await engine.dispose()
        raise

    async with session_factory() as session:
        session.add(
            db.StateNode(
                id=state.state_id,
                run_id=run_id,
                fingerprint=state.fingerprint,
                url=state.url,
                url_normalized=state.url_normalized,
                title=state.title,
                state_type=state.state_type.value,
                screenshot_path=state.screenshot_path,
                dom_snapshot_path=state.dom_snapshot_path,
                text_hash=state.text_hash,
                interactables=[i.model_dump() for i in state.interactables],
                depth=state.depth,
            )
        )
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
