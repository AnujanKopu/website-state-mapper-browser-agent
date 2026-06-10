"""Interactive element discovery and pre-exploration page hygiene.

Discovery runs a single in-page script that finds visible, enabled,
actionable elements, computes a stable CSS selector for each, and records
the context flags (nav / form / modal) the ranking and safety layers need.
"""

from __future__ import annotations

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from engine.schemas import BoundingBox, Interactable

_DISCOVER_JS = """
(maxElements) => {
  const SELECTOR = [
    'a[href]',
    'button',
    'input[type="submit"]',
    'input[type="button"]',
    'select',
    'summary',
    '[role="button"]',
    '[role="link"]',
    '[role="tab"]',
    '[role="menuitem"]',
    '[onclick]',
  ].join(', ');

  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    // checkVisibility also catches content-visibility:hidden (e.g. content
    // of a closed <details>), which keeps a bbox but is not clickable.
    if (el.checkVisibility) {
      try {
        return el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
      } catch {}
    }
    const style = window.getComputedStyle(el);
    return style.display !== 'none'
      && style.visibility !== 'hidden'
      && style.opacity !== '0';
  };

  const isEnabled = (el) => !el.disabled && el.getAttribute('aria-disabled') !== 'true';

  // CSS escape for identifiers (ids may contain anything).
  const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s;

  // Prefer a unique #id; otherwise build a short nth-of-type path.
  const buildSelector = (el) => {
    if (el.id && document.querySelectorAll('#' + esc(el.id)).length === 1) {
      return '#' + esc(el.id);
    }
    const parts = [];
    let node = el;
    while (node && node !== document.body && parts.length < 6) {
      let part = node.tagName.toLowerCase();
      const siblings = node.parentElement
        ? Array.from(node.parentElement.children).filter(c => c.tagName === node.tagName)
        : [];
      if (siblings.length > 1) {
        part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      if (node.parentElement && node.parentElement.id) {
        parts.unshift('#' + esc(node.parentElement.id));
        return parts.join(' > ');
      }
      node = node.parentElement;
    }
    return 'body > ' + parts.join(' > ');
  };

  const truncate = (s, n) => {
    if (!s) return null;
    const trimmed = s.replace(/\\s+/g, ' ').trim();
    if (!trimmed) return null;
    return trimmed.length > n ? trimmed.slice(0, n) : trimmed;
  };

  const results = [];
  const seen = new Set();
  for (const el of document.querySelectorAll(SELECTOR)) {
    if (results.length >= maxElements) break;
    if (!isVisible(el) || !isEnabled(el)) continue;

    const selector = buildSelector(el);
    if (seen.has(selector)) continue;
    seen.add(selector);

    const rect = el.getBoundingClientRect();
    // el.href (the property) resolves relative URLs against the document.
    const href = el.tagName === 'A' && el.href ? el.href : el.getAttribute('href');
    results.push({
      selector,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role'),
      text: truncate(el.innerText || el.value, 120),
      aria_label: truncate(el.getAttribute('aria-label'), 120),
      href,
      in_nav: !!el.closest('nav, header, [role="navigation"]'),
      in_form: !!el.closest('form'),
      in_modal: !!el.closest('[role="dialog"], dialog, [aria-modal="true"]'),
      bounding_box: {
        x: rect.x,
        y: rect.y,
        width: rect.width,
        height: rect.height,
      },
    });
  }
  return results;
}
"""

# Common consent-banner buttons; tried in order, "reject / necessary only"
# first per the safety policy (never opt the user into anything).
_COOKIE_BUTTON_SELECTORS = (
    "#onetrust-reject-all-handler",
    "#CybotCookiebotDialogBodyButtonDecline",
    '[data-testid="uc-deny-all-button"]',
    'button:has-text("Reject all")',
    'button:has-text("Decline")',
    'button:has-text("Necessary only")',
    "#onetrust-accept-btn-handler",
)


async def discover_interactables(page: Page, max_elements: int) -> list[Interactable]:
    """Enumerate actionable elements currently visible on the page."""
    raw: list[dict] = await page.evaluate(_DISCOVER_JS, max_elements)
    return [
        Interactable(
            selector=item["selector"],
            tag=item["tag"],
            role=item["role"],
            text=item["text"],
            aria_label=item["aria_label"],
            href=item["href"],
            in_nav=item["in_nav"],
            in_form=item["in_form"],
            in_modal=item["in_modal"],
            bounding_box=BoundingBox(**item["bounding_box"]),
        )
        for item in raw
    ]


async def dismiss_cookie_banner(page: Page) -> bool:
    """Best-effort dismissal of consent banners so they don't pollute states."""
    for selector in _COOKIE_BUTTON_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible():
                await locator.click(timeout=1_000)
                return True
        except PlaywrightError:
            continue
    return False
