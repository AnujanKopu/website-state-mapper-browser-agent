"""End-to-end capture test against the local fixture site.

Launches real Chromium against a file:// URL -- no network required.
Verifies the full pipeline: browser -> snapshot -> identity -> artifact
storage -> database rows.
"""

from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError
from sqlalchemy import select

from engine.capture import encode_screenshot_artifact, run_single_capture
from engine.config import Settings
from engine.db import models as db
from engine.db.session import create_db_engine, create_session_factory
from engine.storage import LocalStorage
from tests.conftest import FIXTURE_SITE

WEBP_RIFF_MAGIC = b"RIFF"


def test_screenshot_compression_falls_back_to_png_for_invalid_input():
    original = b"not-a-valid-image"
    encoded, extension = encode_screenshot_artifact(original)

    assert encoded == original
    assert extension == "png"


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
    assert state.evidence["page"]["viewport"]["width"] > 0
    assert state.evidence["forms"]
    assert "visible_text" not in state.evidence
    assert all(
        "value" not in field
        for form in state.evidence["forms"]
        for field in form["fields"]
    )

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
    email = next(i for i in state.interactables if i.associated_label == "Email")
    assert email.kind == "text_input"
    assert email.text is None  # never capture the current field value

    # --- artifacts on disk ---
    store = LocalStorage(settings.data_dir)
    screenshot = store.path_for(state.screenshot_path)
    dom = store.path_for(state.dom_snapshot_path)
    screenshot_bytes = screenshot.read_bytes()
    assert screenshot.suffix == ".webp"
    assert screenshot_bytes.startswith(WEBP_RIFF_MAGIC)
    assert screenshot_bytes[8:12] == b"WEBP"
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


async def test_viewport_grounded_surface_discovery(settings: Settings):
    state = await run_single_capture((FIXTURE_SITE / "surface.html").as_uri(), settings)
    items = state.interactables
    by_label = {i.label: i for i in items}
    labels = set(by_label)

    # --- on-screen, unoccluded affordances are discovered ---
    assert {"Get started", "Home", "Features", "Open item 1", "Contact us"} <= labels

    # --- hidden / offscreen / occluded elements are never discovered ---
    assert "Hidden link" not in labels  # display: none
    assert "Offscreen link" not in labels  # painted off the viewport
    assert "Behind overlay" not in labels  # covered by an opaque overlay

    # --- a link wrapping a button collapses to a single surface item ---
    wrapped = [i for i in items if i.label == "Wrapped action"]
    assert len(wrapped) == 1
    assert wrapped[0].tag == "a"
    assert wrapped[0].href and wrapped[0].href.endswith("/wrapped.html")

    # --- regions are tagged from the surrounding landmark ---
    assert by_label["Home"].region == "nav"
    assert by_label["Get started"].region == "main"
    assert by_label["Contact us"].region == "footer"

    # --- below-the-fold items are found via the scroll sweep, tagged fold>0 ---
    footer = by_label["Contact us"]
    assert footer.fold > 0
    assert footer.page_box is not None and footer.page_box.y > 900

    cta = by_label["Get started"]
    assert cta.fold == 0
    assert cta.page_box is not None

    # --- structurally identical siblings share one group id ---
    cards = [i for i in items if i.label.startswith("Open item")]
    assert len(cards) == 3
    assert len({c.group_id for c in cards}) == 1

    # --- every surface item carries a stable id and absolute geometry ---
    assert all(i.item_id and i.page_box is not None for i in items)


async def test_composite_and_icon_controls_have_distinct_semantics(settings: Settings):
    state = await run_single_capture((FIXTURE_SITE / "controls.html").as_uri(), settings)
    items = state.interactables
    labels = {item.label.lower() for item in items}

    assert "chart" in labels
    assert "metrics (7/34)" in labels
    assert "search" in labels
    assert "download" in labels
    assert "table" in labels
    assert any("all categories" in label for label in labels)

    filters = [item for item in items if (item.icon_label or "").lower() == "filter"]
    assert len(filters) == 2
    assert len({item.item_id for item in filters}) == 2
    assert len({item.component_key for item in filters}) == 2
    assert {item.component_label for item in filters} == {"Visits", "Players"}

    icon_buttons = [item for item in items if item.icon_label in {"download", "table"}]
    assert len(icon_buttons) == 2
    assert len({item.item_id for item in icon_buttons}) == 2


async def test_duplicate_ancestor_ids_and_responsive_route_owners_are_preserved(
    settings: Settings,
):
    state = await run_single_capture((FIXTURE_SITE / "navigation.html").as_uri(), settings)
    items = state.interactables
    labels = [item.label for item in items]

    assert {"Home", "Subscriptions", "Music", "Movies & TV", "Live"} <= set(labels)
    assert "Gaming" not in labels  # revealed only after the disclosure is probed
    assert len({item.selector for item in items}) == len(items)
    assert all(not item.selector.startswith("#items >") for item in items if item.label in labels)

    shorts = [item for item in items if item.label == "Shorts"]
    assert len(shorts) == 1
    assert shorts[0].href and shorts[0].href.endswith("/spa.html")
    assert shorts[0].locator["adopted_href"] is True


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
