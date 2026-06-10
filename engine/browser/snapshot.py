"""Page observation: stabilization and snapshot capture."""

from __future__ import annotations

import asyncio
import contextlib

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from engine.schemas import CaptureConfig, PageSnapshot

_NETWORK_IDLE_TIMEOUT_MS = 5_000

# innerText (unlike textContent) reflects rendering: it skips hidden
# elements and collapses layout whitespace, which is what state identity
# should be based on.
_VISIBLE_TEXT_JS = "() => document.body ? document.body.innerText : ''"


async def stabilize(page: Page, quiet_ms: int) -> None:
    """Wait until the page is reasonably settled before observing it.

    Network-idle is best-effort: long-polling and analytics beacons keep
    some pages permanently "busy", so a timeout there is not an error.
    """
    await page.wait_for_load_state("domcontentloaded")
    with contextlib.suppress(PlaywrightTimeoutError):
        await page.wait_for_load_state("networkidle", timeout=_NETWORK_IDLE_TIMEOUT_MS)
    await asyncio.sleep(quiet_ms / 1000)


async def take_snapshot(page: Page, config: CaptureConfig) -> PageSnapshot:
    """Capture the observable surface of the current page."""
    visible_text: str = await page.evaluate(_VISIBLE_TEXT_JS)
    if len(visible_text) > config.max_visible_text_chars:
        visible_text = visible_text[: config.max_visible_text_chars]

    return PageSnapshot(
        url=page.url,
        title=await page.title(),
        visible_text=visible_text,
        html=await page.content(),
        screenshot_png=await page.screenshot(full_page=config.full_page_screenshot),
    )
