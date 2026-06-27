"""Interactive element discovery and pre-exploration page hygiene.

Discovery is viewport-grounded: an in-page script finds elements that are
*actually* visible right now -- on-screen, large enough to hit, not occluded
by an overlay -- and records the region (nav/header/footer/aside/modal/main),
a coarse kind, and both viewport- and document-relative geometry. A Python
scroll sweep runs the script at successive scroll offsets so below-the-fold
affordances are discovered too, each tagged with the fold they appeared at.
"""

from __future__ import annotations

import contextlib
import hashlib
from urllib.parse import urljoin

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from engine.identity import strip_positional_selector
from engine.ranking import group_signature
from engine.safety import is_same_origin
from engine.schemas import BoundingBox, Interactable

# Discovery script. Returns only elements that are visible, enabled, large
# enough, inside the current viewport, and not occluded by another element.
# Geometry is reported both viewport-relative (rect) and document-relative
# (page_box = rect + scroll offset) for stable, screenshot-aligned coords.
_DISCOVER_JS = """
(maxElements) => {
  const SELECTOR = [
    'a[href]',
    'button',
    'input[type="submit"]',
    'input[type="button"]',
    'input[type="checkbox"]',
    'input[type="radio"]',
    'select',
    'summary',
    '[role="button"]',
    '[role="link"]',
    '[role="tab"]',
    '[role="menuitem"]',
    '[onclick]',
  ].join(', ');

  const MIN_SIZE = 8;
  const vw = window.innerWidth;
  const vh = window.innerHeight;

  const isVisible = (el) => {
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

  const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s;

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

  const contextLabelOf = (el) => {
    const owner = el.closest('section, article, li, [role="region"], [data-testid]');
    if (!owner) return null;
    const heading = owner.querySelector('h1, h2, h3, [role="heading"]');
    return truncate(
      (heading && heading.innerText)
        || owner.getAttribute('aria-label')
        || owner.getAttribute('data-testid'),
      80,
    );
  };

  const regionOf = (el) => {
    if (el.closest('[role="dialog"], dialog, [aria-modal="true"]')) return 'modal';
    if (el.closest('nav, [role="navigation"]')) return 'nav';
    if (el.closest('header')) return 'header';
    if (el.closest('footer')) return 'footer';
    if (el.closest('aside')) return 'aside';
    if (el.closest('main')) return 'main';
    return null;
  };

  const kindOf = (el, tag, role) => {
    if (role === 'tab') return 'tab';
    if (role === 'menuitem') return 'menuitem';
    if (tag === 'select') return 'select';
    if (tag === 'summary') return 'disclosure';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || '').toLowerCase();
      if (t === 'checkbox' || t === 'radio') return 'toggle';
      return 'button';
    }
    if (tag === 'a') return 'link';
    return 'button';
  };

  const sx = window.scrollX;
  const sy = window.scrollY;
  const results = [];
  const seen = new Set();
  for (const el of document.querySelectorAll(SELECTOR)) {
    if (results.length >= maxElements) break;
    if (!isVisible(el) || !isEnabled(el)) continue;

    const rect = el.getBoundingClientRect();
    if (rect.width < MIN_SIZE || rect.height < MIN_SIZE) continue;

    const style = window.getComputedStyle(el);
    if (style.pointerEvents === 'none') continue;

    // Viewport intersection: center on-screen, or >=50% of the area visible.
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const ix0 = Math.max(0, rect.left), iy0 = Math.max(0, rect.top);
    const ix1 = Math.min(vw, rect.right), iy1 = Math.min(vh, rect.bottom);
    const visArea = Math.max(0, ix1 - ix0) * Math.max(0, iy1 - iy0);
    const area = rect.width * rect.height;
    const centerIn = cx >= 0 && cx <= vw && cy >= 0 && cy <= vh;
    if (!centerIn && !(area > 0 && visArea / area >= 0.5)) continue;
    if (visArea <= 0) continue;

    // Occlusion: hit-test a point inside the visible part of the element.
    const hx = Math.min(Math.max(cx, ix0 + 1), ix1 - 1);
    const hy = Math.min(Math.max(cy, iy0 + 1), iy1 - 1);
    const hit = document.elementFromPoint(hx, hy);
    const unoccluded = hit && (hit === el || el.contains(hit) || hit.contains(el));
    if (!unoccluded) continue;

    // Nested-actionable dedupe: when a link wraps a button (or vice versa),
    // let the element actually painted under the cursor represent the pair.
    if (hit !== el && hit.matches && hit.matches(SELECTOR)
        && (el.contains(hit) || hit.contains(el))) {
      continue;
    }

    const selector = buildSelector(el);
    if (seen.has(selector)) continue;
    seen.add(selector);

    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role');
    const href = el.tagName === 'A' && el.href ? el.href : el.getAttribute('href');
    results.push({
      selector,
      tag,
      role,
      text: truncate(el.innerText || el.value, 120),
      aria_label: truncate(el.getAttribute('aria-label'), 120),
      aria_selected: el.hasAttribute('aria-selected')
        ? el.getAttribute('aria-selected') === 'true'
        : null,
      aria_expanded: el.hasAttribute('aria-expanded')
        ? el.getAttribute('aria-expanded') === 'true'
        : null,
      aria_controls: truncate(el.getAttribute('aria-controls'), 160),
      aria_haspopup: truncate(el.getAttribute('aria-haspopup'), 40),
      aria_pressed: el.hasAttribute('aria-pressed')
        ? el.getAttribute('aria-pressed') === 'true'
        : null,
      checked: ('checked' in el) ? !!el.checked : null,
      input_type: truncate(el.getAttribute('type'), 40),
      required: !!el.required || el.getAttribute('aria-required') === 'true',
      autocomplete: truncate(el.getAttribute('autocomplete'), 80),
      form_action: el.form ? (el.form.action || null) : null,
      form_method: el.form ? (el.form.method || 'get').toLowerCase() : null,
      title: truncate(el.getAttribute('title'), 120),
      test_id: truncate(el.getAttribute('data-testid'), 120),
      context_label: contextLabelOf(el),
      href,
      in_nav: !!el.closest('nav, header, [role="navigation"]'),
      in_form: !!el.closest('form'),
      in_modal: !!el.closest('[role="dialog"], dialog, [aria-modal="true"]'),
      region: regionOf(el),
      container_selector: (() => {
        const owner = el.closest([
          '[role="tablist"]', 'nav', '[role="navigation"]', 'header',
          'aside', 'footer', '[role="dialog"]', 'dialog'
        ].join(', '));
        return owner ? buildSelector(owner) : null;
      })(),
      kind: kindOf(el, tag, role),
      bounding_box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      page_box: { x: rect.x + sx, y: rect.y + sy, width: rect.width, height: rect.height },
    });
  }
  return {
    items: results,
    page_height: document.body ? document.body.scrollHeight : 0,
  };
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


def _item_id(selector: str, label: str) -> str:
    """Stable id for a surface item across folds and re-observations."""
    basis = f"{strip_positional_selector(selector)}|{label}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def _semantic_key(*parts: str | None) -> str:
    basis = "|".join((part or "").strip().lower() for part in parts)
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def _build_interactable(raw: dict, fold: int) -> Interactable:
    page_box = raw.get("page_box")
    item = Interactable(
        selector=raw["selector"],
        tag=raw["tag"],
        role=raw["role"],
        text=raw["text"],
        aria_label=raw["aria_label"],
        aria_selected=raw.get("aria_selected"),
        aria_expanded=raw.get("aria_expanded"),
        aria_controls=raw.get("aria_controls"),
        aria_haspopup=raw.get("aria_haspopup"),
        aria_pressed=raw.get("aria_pressed"),
        checked=raw.get("checked"),
        input_type=raw.get("input_type"),
        required=bool(raw.get("required")),
        autocomplete=raw.get("autocomplete"),
        form_action=raw.get("form_action"),
        form_method=raw.get("form_method"),
        title=raw.get("title"),
        test_id=raw.get("test_id"),
        context_label=raw.get("context_label"),
        href=raw["href"],
        in_nav=raw["in_nav"],
        in_form=raw["in_form"],
        in_modal=raw["in_modal"],
        region=raw.get("region"),
        kind=raw.get("kind"),
        fold=fold,
        bounding_box=BoundingBox(**raw["bounding_box"]),
        page_box=BoundingBox(**page_box) if page_box else None,
    )
    item.item_id = _item_id(item.selector, item.label)
    item.group_id = group_signature(item)
    container_shape = strip_positional_selector(raw.get("container_selector") or "")
    item.container_key = _semantic_key(item.region, container_shape)
    item.control_key = _semantic_key(
        item.kind,
        item.region,
        item.label,
        item.href,
        strip_positional_selector(item.selector),
        item.container_key,
    )
    return item


async def discover_interactables(
    page: Page, max_elements: int, max_scroll_steps: int = 0
) -> list[Interactable]:
    """Enumerate actionable elements visible across a top-down scroll sweep.

    The discovery script only reports what is on-screen and unoccluded, so the
    page is scrolled in ~90%-viewport steps to surface below-the-fold items.
    The first fold an element appears at is recorded; the page is restored to
    the top when done. Scrolling never changes page identity.
    """
    viewport = page.viewport_size or {"width": 0, "height": 900}
    step_px = max(1, int(viewport["height"] * 0.9))
    seen: dict[str, Interactable] = {}
    order: list[str] = []

    fold = 0
    offset = 0
    while True:
        if fold > 0:
            await page.evaluate(
                """(y) => new Promise((resolve) => {
                    window.scrollTo(0, y);
                    requestAnimationFrame(() => requestAnimationFrame(resolve));
                })""",
                offset,
            )
        observed: dict = await page.evaluate(_DISCOVER_JS, max_elements)
        raw: list[dict] = observed["items"]
        for entry in raw:
            selector = entry["selector"]
            if selector in seen:
                continue
            seen[selector] = _build_interactable(entry, fold)
            order.append(selector)
            if len(seen) >= max_elements:
                break

        page_height: float = observed["page_height"]
        reached_bottom = offset + viewport["height"] >= page_height
        if len(seen) >= max_elements or fold >= max_scroll_steps or reached_bottom:
            break
        fold += 1
        offset += step_px

    if fold > 0:
        await page.evaluate(
            """() => new Promise((resolve) => {
                window.scrollTo(0, 0);
                requestAnimationFrame(() => requestAnimationFrame(resolve));
            })"""
        )

    return [seen[selector] for selector in order][:max_elements]


async def click_interactable(
    page: Page,
    item: Interactable,
    *,
    timeout_ms: int,
    base_url: str | None = None,
) -> None:
    """Perform a discovered action: navigate by href when possible, else click.

    SPAs (Next.js, React Router) often break generated CSS selectors after
    hydration. Same-origin ``<a href>`` navigation is tried first because it
    is more reliable than ``page.locator(generated-css).click()``.
    """
    href = (item.href or "").strip()
    if (
        item.tag == "a"
        and href
        and not href.startswith("#")
        and not href.startswith(("javascript:", "mailto:", "tel:"))
    ):
        target = urljoin(page.url, href)
        origin = base_url or page.url
        source_url = page.url
        if is_same_origin(target, origin):
            try:
                await page.goto(target, timeout=timeout_ms, wait_until="domcontentloaded")
                return
            except PlaywrightError:
                # A navigation can time out after Chromium has already moved to
                # the target. Let the explorer observe that result instead of
                # trying the old selector on the new document.
                if page.url != source_url:
                    return
                pass  # fall through to locator strategies

    locator = page.locator(item.selector).first
    try:
        await locator.scroll_into_view_if_needed(timeout=timeout_ms)
        await locator.click(timeout=timeout_ms)
        return
    except PlaywrightError:
        pass

    label = item.text or item.aria_label
    if label:
        for role in filter(None, [item.role, "link" if item.tag == "a" else None, "button"]):
            try:
                role_loc = page.get_by_role(role, name=label, exact=False).first
                await role_loc.click(timeout=timeout_ms)
                return
            except PlaywrightError:
                continue

    await locator.click(timeout=timeout_ms, force=True)


async def validate_interactable(page: Page, item: Interactable) -> bool:
    """Cheaply reject selectors that now point at a different control."""
    # Same-origin anchors are executed by URL first, deliberately avoiding
    # generated selectors that often change after hydration.
    if item.tag == "a" and item.href and not item.href.startswith("#"):
        return True
    try:
        locator = page.locator(item.selector)
        if await locator.count() < 1:
            label = item.text or item.aria_label or item.title
            role = item.role or ("link" if item.tag == "a" else "button")
            if label:
                return await page.get_by_role(role, name=label, exact=False).count() == 1
            return False
        current = locator.first
        if item.href:
            href = await current.get_attribute("href")
            if href and urljoin(page.url, href) != urljoin(page.url, item.href):
                return False
        expected = (item.text or item.aria_label or item.title or "").strip().lower()
        if expected:
            current_text = ""
            with contextlib.suppress(PlaywrightError):
                current_text = (await current.inner_text(timeout=500)).strip().lower()
            if not current_text:
                current_text = (
                    (await current.get_attribute("aria-label"))
                    or (await current.get_attribute("title"))
                    or ""
                ).strip().lower()
            if current_text and expected not in current_text and current_text not in expected:
                return False
        return True
    except PlaywrightError:
        return False


async def click_selector(
    page: Page,
    selector: str,
    *,
    timeout_ms: int,
    label: str | None = None,
    role: str | None = None,
    href: str | None = None,
) -> None:
    """Replay a stored path step by selector."""
    locator = page.locator(selector).first
    try:
        await locator.scroll_into_view_if_needed(timeout=timeout_ms)
        await locator.click(timeout=timeout_ms)
        return
    except PlaywrightError as original_error:
        if href:
            target = urljoin(page.url, href)
            if is_same_origin(target, page.url):
                await page.goto(target, timeout=timeout_ms, wait_until="domcontentloaded")
                return
        if label and role:
            semantic = page.get_by_role(role, name=label, exact=False)
            if await semantic.count() == 1:
                await semantic.click(timeout=timeout_ms)
                return
        try:
            await locator.click(timeout=timeout_ms, force=True)
            return
        except PlaywrightError:
            raise original_error from None


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
