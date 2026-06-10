"""Page observation: stabilization, snapshot, DOM skeleton, and signals."""

from __future__ import annotations

import asyncio
import contextlib

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from engine.schemas import CaptureConfig, PageSignals, PageSnapshot

_NETWORK_IDLE_TIMEOUT_MS = 5_000

# innerText (unlike textContent) reflects rendering: it skips hidden
# elements and collapses layout whitespace, which is what state identity
# should be based on.
_VISIBLE_TEXT_JS = "() => document.body ? document.body.innerText : ''"

# Builds a text-free skeleton of the *visible* DOM (tag + role tree, runs of
# more than 3 same-tag siblings truncated) and extracts classification
# signals. One evaluate() round-trip.
_OBSERVE_JS = """
() => {
  const SKIP_TAGS = new Set(['script', 'style', 'link', 'meta', 'noscript', 'template', 'svg']);
  const MAX_NODES = 1500;
  let nodeCount = 0;

  const isVisible = (el) => {
    try {
      return el.checkVisibility
        ? el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })
        : true;
    } catch {
      return true;
    }
  };

  const skeletonOf = (el, depth) => {
    if (nodeCount >= MAX_NODES || depth > 12) return '';
    const tag = el.tagName.toLowerCase();
    if (SKIP_TAGS.has(tag)) return '';
    if (!isVisible(el)) return '';
    nodeCount++;

    let token = tag;
    const role = el.getAttribute('role');
    if (role) token += '[' + role + ']';

    const childTokens = [];
    let lastTag = '';
    let run = 0;
    for (const child of el.children) {
      if (child.tagName === lastTag) {
        run++;
        if (run > 3) continue;  // truncate long sibling lists (cards, rows)
      } else {
        lastTag = child.tagName;
        run = 1;
      }
      const childSkeleton = skeletonOf(child, depth + 1);
      if (childSkeleton) childTokens.push(childSkeleton);
    }
    return childTokens.length ? token + '(' + childTokens.join(',') + ')' : token;
  };

  const visibleAll = (selector) =>
    Array.from(document.querySelectorAll(selector)).filter(isVisible);

  const paymentRe = /card.?number|cardnumber|cvc|cvv|expir/i;
  const paymentFields = Array.from(document.querySelectorAll('input')).filter((el) =>
    isVisible(el) && (
      paymentRe.test(el.name || '') ||
      paymentRe.test(el.id || '') ||
      paymentRe.test(el.placeholder || '') ||
      (el.getAttribute('autocomplete') || '').startsWith('cc-')
    )
  ).length;

  return {
    skeleton: document.body ? skeletonOf(document.body, 0) : '',
    signals: {
      modal_open: visibleAll('[role="dialog"], dialog[open], [aria-modal="true"]').length > 0,
      password_fields: visibleAll('input[type="password"]').length,
      payment_fields: paymentFields,
      form_count: visibleAll('form').length,
    },
  };
}
"""


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

    observed: dict = await page.evaluate(_OBSERVE_JS)

    return PageSnapshot(
        url=page.url,
        title=await page.title(),
        visible_text=visible_text,
        html=await page.content(),
        screenshot_png=await page.screenshot(full_page=config.full_page_screenshot),
        dom_skeleton=observed["skeleton"],
        signals=PageSignals(**observed["signals"]),
    )
