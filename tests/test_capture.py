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

FIXTURE = Path(__file__).parent / "fixtures" / "demo_site" / "index.html"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
        data_dir=tmp_path / "artifacts",
        run_config_path=tmp_path / "missing.yaml",  # falls back to RunConfig defaults
    )


async def test_single_capture_end_to_end(settings: Settings):
    state = await run_single_capture(FIXTURE.as_uri(), settings)

    # --- identity ---
    assert state.title == "FlowState Demo Site"
    assert len(state.fingerprint) == 16

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
    assert pricing.href == "/pricing"
    assert pricing.bounding_box.width > 0

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
