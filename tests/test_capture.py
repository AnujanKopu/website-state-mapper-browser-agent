"""End-to-end capture test against the local fixture site.

Launches real Chromium against a file:// URL -- no network required.
Verifies the full pipeline: browser -> snapshot -> identity -> artifact
storage -> database rows.
"""

from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError
from sqlalchemy import select

from engine.capture import run_single_capture
from engine.config import Settings
from engine.db import models as db
from engine.db.session import create_db_engine, create_session_factory
from engine.storage import LocalStorage
from tests.conftest import FIXTURE_SITE

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


async def test_single_capture_end_to_end(settings: Settings):
    state = await run_single_capture((FIXTURE_SITE / "index.html").as_uri(), settings)

    # --- identity ---
    assert state.title == "FlowState Demo Site"
    assert len(state.fingerprint) == 16
    assert state.skeleton_hash
    assert state.action_sig

    # --- page signals ---
    assert state.signals.modal_open is False
    assert state.signals.form_count >= 1
    assert state.state_type.value == "page"

    # --- visible text (hidden elements must be excluded) ---
    assert "FlowState Demo" in state.visible_text
    assert "Hidden link" not in state.visible_text

    # --- interactables ---
    labels = {i.label for i in state.interactables}
    assert "Pricing" in labels
    assert "Sign up" in labels
    assert "Send message" in labels
    assert "Hidden link" not in labels  # display: none
    assert "Disabled button" not in labels  # disabled
    assert "Close" not in labels  # inside the closed modal

    pricing = next(i for i in state.interactables if i.label == "Pricing")
    assert pricing.href and pricing.href.endswith("pricing.html")  # resolved absolute
    assert pricing.in_nav
    assert pricing.bounding_box.width > 0

    submit = next(i for i in state.interactables if i.label == "Send message")
    assert submit.in_form

    # --- artifacts on disk ---
    store = LocalStorage(settings.data_dir)
    screenshot = store.path_for(state.screenshot_path)
    dom = store.path_for(state.dom_snapshot_path)
    assert screenshot.read_bytes().startswith(PNG_MAGIC)
    assert "signup-modal" in dom.read_text(encoding="utf-8")

    # --- database rows ---
    engine = create_db_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        run = await session.get(db.Run, state.run_id)
        assert run is not None
        assert run.status == "done"
        assert run.stats["states"] == 1
        assert run.finished_at is not None

        rows = (await session.execute(select(db.StateNode))).scalars().all()
        assert len(rows) == 1
        node = rows[0]
        assert node.fingerprint == state.fingerprint
        assert node.state_type == "page"
        assert node.dom_skeleton_hash == state.skeleton_hash
        assert any(i["text"] == "Sign up" for i in node.interactables)
    await engine.dispose()


async def test_failed_navigation_marks_run_failed(settings: Settings, tmp_path: Path):
    bad_url = (tmp_path / "does_not_exist.html").as_uri()
    with pytest.raises(PlaywrightError):
        await run_single_capture(bad_url, settings)

    engine = create_db_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        runs = (await session.execute(select(db.Run))).scalars().all()
        assert len(runs) == 1
        assert runs[0].status == "failed"
        assert runs[0].error
    await engine.dispose()
