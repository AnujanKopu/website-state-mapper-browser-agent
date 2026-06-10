"""Interactive element discovery.

Runs a single in-page script that finds visible, enabled, actionable
elements and computes a stable CSS selector for each. One evaluate()
round-trip keeps capture fast regardless of element count.
"""

from __future__ import annotations

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
    results.push({
      selector,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role'),
      text: truncate(el.innerText || el.value, 120),
      aria_label: truncate(el.getAttribute('aria-label'), 120),
      href: el.getAttribute('href'),
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
            bounding_box=BoundingBox(**item["bounding_box"]),
        )
        for item in raw
    ]
