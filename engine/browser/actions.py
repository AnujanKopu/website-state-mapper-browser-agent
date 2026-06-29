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
import re
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
    'a',
    'a[href]',
    'button',
    'input:not([type="hidden"])',
    'textarea',
    'select',
    'summary',
    '[contenteditable="true"]',
    '[role="button"]',
    '[role="link"]',
    '[role="tab"]',
    '[role="menuitem"]',
    '[role="searchbox"]',
    '[role="combobox"]',
    '[role="checkbox"]',
    '[role="radio"]',
    '[role="switch"]',
    '[aria-expanded]',
    '[aria-controls]',
    '[aria-haspopup]',
    '[aria-pressed]',
    '[tabindex]:not([tabindex="-1"])',
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

  const selectorCount = (selector) => {
    try { return document.querySelectorAll(selector).length; } catch { return 0; }
  };

  const selectorPart = (node) => {
    let part = node.tagName.toLowerCase();
    const siblings = node.parentElement
      ? Array.from(node.parentElement.children).filter(c => c.tagName === node.tagName)
      : [];
    if (siblings.length > 1) {
      part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
    }
    return part;
  };

  const domInstancePathOf = (el) => {
    const parts = [];
    let node = el;
    while (node && node !== document.body) {
      parts.unshift(selectorPart(node));
      node = node.parentElement;
    }
    return 'body > ' + parts.join(' > ');
  };

  const buildSelector = (el) => {
    if (el.id) {
      const ownId = '#' + esc(el.id);
      if (selectorCount(ownId) === 1) return ownId;
    }
    const parts = [];
    let node = el;
    while (node && node !== document.body) {
      parts.unshift(selectorPart(node));

      // An ancestor ID is useful only when the ID itself is unique and the
      // complete anchored selector still identifies exactly this element.
      const parent = node.parentElement;
      if (parent?.id) {
        const parentId = '#' + esc(parent.id);
        const anchored = `${parentId} > ${parts.join(' > ')}`;
        if (selectorCount(parentId) === 1 && selectorCount(anchored) === 1) {
          return anchored;
        }
      }

      const candidate = 'body > ' + parts.join(' > ');
      if (selectorCount(candidate) === 1) return candidate;
      node = parent;
    }
    // A full nth-of-type path is deterministic and unique in one rendered
    // document even when frameworks repeat IDs such as #items/#endpoint.
    return domInstancePathOf(el);
  };

  const truncate = (s, n) => {
    if (!s) return null;
    const trimmed = s.replace(/\\s+/g, ' ').trim();
    if (!trimmed) return null;
    return trimmed.length > n ? trimmed.slice(0, n) : trimmed;
  };

  const contextLabelOf = (el) => {
    const owner = el.closest(
      'th, td, label, form, table, [role="toolbar"], [role="tablist"], section, article, li, '
      + '[role="region"], [data-testid]'
    );
    if (!owner) return null;
    const heading = owner.querySelector('h1, h2, h3, [role="heading"]');
    return truncate(
      (heading && heading.innerText)
        || owner.getAttribute('aria-label')
        || owner.getAttribute('data-testid')
        || (owner.matches('th, td, label') ? owner.innerText : null),
      80,
    );
  };

  const iconLabelOf = (el) => {
    const child = el.querySelector(
      '[data-icon], svg, img[alt], [class*="icon" i], svg title, svg desc'
    );
    const values = [
      child?.getAttribute?.('data-icon'),
      child?.getAttribute?.('aria-label'),
      child?.getAttribute?.('alt'),
      child?.textContent,
      el.getAttribute('data-icon'),
    ];
    for (const value of values) {
      const label = truncate(value, 80);
      if (label) return label.replace(/[-_]+/g, ' ');
    }
    const classText = `${el.className || ''} ${child?.getAttribute?.('class') || ''}`;
    const match = classText.match(
      /\b(download|search|filter|sort|chart|table|columns?|layout|settings?|calendar|menu)\b/i
    );
    return match ? match[1] : null;
  };

  const componentOwnerOf = (el) => {
    const tableHeader = el.closest('th');
    if (tableHeader) return tableHeader;
    const search = el.closest('[role="search"]');
    if (search) return search;
    if (el.matches('[role="combobox"]')) {
      const compactOwner = el.parentElement;
      const ownerText = truncate(compactOwner?.innerText, 120);
      const ownerInputs = compactOwner?.querySelectorAll('[role="combobox"], input, select');
      if (compactOwner && ownerText && ownerInputs?.length === 1) {
        return compactOwner;
      }
    }
    if (el.matches('[aria-controls], [aria-haspopup], [role="tab"], [role="menuitem"]')) {
      return el;
    }
    if (el.matches('[role="combobox"], input, textarea')) {
      const pointerOwner = el.parentElement?.closest('div, span, label');
      if (pointerOwner && window.getComputedStyle(pointerOwner).cursor === 'pointer') {
        return pointerOwner;
      }
    }
    const compact = el.parentElement;
    if (compact) {
      const controls = compact.querySelectorAll('input, button, [role="button"]');
      if (controls.length > 1 && controls.length <= 3 && compact.querySelector('input')) {
        return compact;
      }
    }
    return el;
  };

  const actionOwnerOf = (el) => {
    // Frameworks often put the accessible name/icon on a span or button
    // nested inside the element that actually owns navigation.  Preserve the
    // outer route owner and read its visible descendants for the label.
    const routedAnchor = el.closest('a[href]');
    if (routedAnchor) return routedAnchor;
    if (el.matches('a:not([href])')) {
      const parentOwner = el.parentElement?.closest([
        'button', '[role="link"]', '[role="button"]', '[role="tab"]',
        '[role="menuitem"]', '[aria-controls]', '[aria-haspopup]',
        '[aria-expanded]', '[onclick]'
      ].join(', '));
      if (parentOwner) return parentOwner;
    }
    const semanticOwner = el.closest([
      'button', 'a', 'summary', 'input', 'textarea', 'select',
      '[role="link"]', '[role="button"]', '[role="tab"]',
      '[role="menuitem"]', '[aria-controls]', '[aria-haspopup]',
      '[aria-expanded]', '[onclick]'
    ].join(', '));
    if (semanticOwner) return semanticOwner;
    // Cursor is inherited, so a text span inside a framework click target may
    // also look clickable. Walk to the highest contiguous pointer owner to
    // canonicalize the component without swallowing its surrounding layout.
    let pointerOwner = null;
    let node = el;
    for (let depth = 0; node && depth < 6; depth += 1, node = node.parentElement) {
      if (window.getComputedStyle(node).cursor !== 'pointer') break;
      pointerOwner = node;
    }
    return pointerOwner || el;
  };

  const normalizedLabelOf = (el) => (
    el.getAttribute('aria-label')
      || el.getAttribute('title')
      || el.innerText
      || ''
  ).replace(/\\s+/g, ' ').trim().toLowerCase();

  const overlapRatio = (left, right) => {
    const x0 = Math.max(left.left, right.left);
    const y0 = Math.max(left.top, right.top);
    const x1 = Math.min(left.right, right.right);
    const y1 = Math.min(left.bottom, right.bottom);
    const intersection = Math.max(0, x1 - x0) * Math.max(0, y1 - y0);
    const smaller = Math.min(left.width * left.height, right.width * right.height);
    return smaller > 0 ? intersection / smaller : 0;
  };

  const associatedHrefOf = (el) => {
    const direct = el.getAttribute('href');
    if (direct) return { href: direct, adopted: false };
    const related = el.closest('a[href]') || el.querySelector?.('a[href]');
    if (related?.getAttribute('href')) {
      return { href: related.getAttribute('href'), adopted: related !== el };
    }

    // Responsive/custom-element renderers can paint a semantic copy over the
    // actual route anchor. Adopt a target only when exactly one same-label
    // anchor substantially overlaps inside the same shell component.
    const label = normalizedLabelOf(el);
    if (!label) return { href: null, adopted: false };
    const rect = el.getBoundingClientRect();
    const shell = el.closest('nav, header, aside, main, [role="navigation"]');
    const matches = Array.from(document.querySelectorAll('a[href]')).filter((anchor) => {
      if (anchor === el || normalizedLabelOf(anchor) !== label) return false;
      const anchorShell = anchor.closest('nav, header, aside, main, [role="navigation"]');
      if (shell && anchorShell && shell !== anchorShell) return false;
      return overlapRatio(rect, anchor.getBoundingClientRect()) >= 0.8;
    });
    return matches.length === 1
      ? { href: matches[0].getAttribute('href'), adopted: true }
      : { href: null, adopted: false };
  };

  const associatedLabelOf = (el) => {
    const labelledBy = (el.getAttribute('aria-labelledby') || '')
      .split(/\\s+/)
      .filter(Boolean)
      .map((id) => document.getElementById(id)?.innerText || '')
      .join(' ');
    if (labelledBy) return truncate(labelledBy, 120);
    if (el.id) {
      const label = document.querySelector(`label[for="${esc(el.id)}"]`);
      if (label?.innerText) return truncate(label.innerText, 120);
    }
    const wrapping = el.closest('label');
    return wrapping ? truncate(wrapping.innerText, 120) : null;
  };

  const textOf = (el, tag) => {
    if (tag !== 'input') return truncate(el.innerText, 120);
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    return ['button', 'submit', 'reset'].includes(type) ? truncate(el.value, 120) : null;
  };

  const controlledSurfaceOf = (el) => {
    const href = el.getAttribute('href') || '';
    const raw = el.getAttribute('aria-controls')
      || el.getAttribute('data-target')
      || el.getAttribute('data-bs-target')
      || (href.startsWith('#') ? href.slice(1) : '');
    if (!raw) return null;
    const id = raw.replace(/^#/, '').split(/\\s+/)[0];
    const target = document.getElementById(id);
    if (!target) return { id, role: null, visible: null };
    return {
      id,
      role: target.getAttribute('role') || target.tagName.toLowerCase(),
      visible: isVisible(target),
    };
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
    if (role === 'searchbox') return 'search';
    if (role === 'combobox') return 'select';
    if (role === 'switch' || role === 'checkbox' || role === 'radio') return 'toggle';
    if (tag === 'select') return 'select';
    if (tag === 'textarea' || el.isContentEditable) return 'text_input';
    if (tag === 'summary') return 'disclosure';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || '').toLowerCase();
      if (t === 'checkbox' || t === 'radio') return 'toggle';
      if (t === 'search') return 'search';
      if (['button', 'submit', 'reset'].includes(t)) return 'button';
      return 'text_input';
    }
    if (tag === 'a') return 'link';
    return 'button';
  };

  const sx = window.scrollX;
  const sy = window.scrollY;
  const results = [];
  const rawCandidates = new Set(document.querySelectorAll(SELECTOR));
  // React and other frameworks frequently attach handlers without an onclick
  // attribute. A visible pointer-cursor owner is useful structural evidence,
  // but only promote elements with text, an icon, or a semantic descendant.
  for (const el of document.querySelectorAll('div, span, li')) {
    if (rawCandidates.size >= maxElements * 3) break;
    if (window.getComputedStyle(el).cursor !== 'pointer') continue;
    if (!(truncate(el.innerText, 120) || iconLabelOf(el)
        || el.querySelector('[role="combobox"], input, [aria-expanded], [aria-controls]'))) {
      continue;
    }
    rawCandidates.add(el);
  }

  const candidates = new Set(
    Array.from(rawCandidates, (candidate) => actionOwnerOf(candidate))
  );

  for (const el of candidates) {
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

    const selector = buildSelector(el);
    const domInstancePath = domInstancePathOf(el);

    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role');
    const associatedRoute = associatedHrefOf(el);
    const href = associatedRoute.href
      ? new URL(associatedRoute.href, document.baseURI).href
      : null;
    const iconLabel = iconLabelOf(el);
    const componentOwner = componentOwnerOf(el);
    results.push({
      selector,
      dom_instance_path: domInstancePath,
      locator: {
        css: selector,
        dom_path: domInstancePath,
        selector_unique: selectorCount(selector) === 1,
        duplicate_id_ancestor: !!el.closest('[id]')
          && Array.from(el.closest('[id]') ? [el.closest('[id]')] : [])
            .some((owner) => selectorCount('#' + esc(owner.id)) > 1),
        adopted_href: associatedRoute.adopted,
      },
      tag,
      role,
      text: textOf(el, tag),
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
      placeholder: truncate(el.getAttribute('placeholder'), 120),
      name: truncate(el.getAttribute('name'), 120),
      associated_label: associatedLabelOf(el),
      input_type: truncate(el.getAttribute('type'), 40),
      required: !!el.required || el.getAttribute('aria-required') === 'true',
      autocomplete: truncate(el.getAttribute('autocomplete'), 80),
      form_action: el.form ? (el.form.action || null) : null,
      form_method: el.form ? (el.form.method || 'get').toLowerCase() : null,
      title: truncate(el.getAttribute('title'), 120),
      test_id: truncate(el.getAttribute('data-testid'), 120),
      context_label: contextLabelOf(el),
      icon_label: iconLabel,
      href,
      download: el.hasAttribute('download'),
      in_nav: !!el.closest('nav, header, [role="navigation"]'),
      in_form: !!el.closest('form'),
      in_modal: !!el.closest('[role="dialog"], dialog, [aria-modal="true"]'),
      region: regionOf(el),
      container_selector: (() => {
        const owner = el.closest([
          '[role="tablist"]', 'nav', '[role="navigation"]', 'header',
          'aside', 'footer', '[role="dialog"]', 'dialog', 'form', 'table',
          '[role="toolbar"]'
        ].join(', '));
        return owner ? buildSelector(owner) : null;
      })(),
      container_type: (() => {
        const owner = el.closest([
          '[role="tablist"]', 'nav', '[role="navigation"]', 'header',
          'aside', 'footer', '[role="dialog"]', 'dialog', 'form', 'table',
          '[role="toolbar"]'
        ].join(', '));
        return owner ? (owner.getAttribute('role') || owner.tagName.toLowerCase()) : null;
      })(),
      component_selector: buildSelector(componentOwner),
      component_label: truncate(
        componentOwner.getAttribute('aria-label')
          || componentOwner.querySelector('legend, caption, h1, h2, h3, [role="heading"]')
            ?.textContent
          || textOf(componentOwner, componentOwner.tagName.toLowerCase())
          || textOf(el, tag)
          || el.getAttribute('aria-label')
          || associatedLabelOf(el)
          || el.getAttribute('placeholder')
          || iconLabel
          || contextLabelOf(el),
        120,
      ),
      controlled_surface: controlledSurfaceOf(el),
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


def _item_id(
    dom_instance_path: str,
    label: str,
    href: str | None,
    role: str | None,
    container_key: str | None,
) -> str:
    """Stable id for a surface item across folds and re-observations."""
    # The exact DOM path distinguishes sibling instances but is not used as a
    # cross-state semantic capability key.
    basis = "|".join(
        (dom_instance_path, label, href or "", role or "", container_key or "")
    )
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def _semantic_key(*parts: str | None) -> str:
    basis = "|".join((part or "").strip().lower() for part in parts)
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def _build_interactable(raw: dict, fold: int) -> Interactable:
    page_box = raw.get("page_box")
    item = Interactable(
        selector=raw["selector"],
        dom_instance_path=raw.get("dom_instance_path") or raw["selector"],
        locator=dict(raw.get("locator") or {}),
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
        placeholder=raw.get("placeholder"),
        name=raw.get("name"),
        associated_label=raw.get("associated_label"),
        input_type=raw.get("input_type"),
        required=bool(raw.get("required")),
        autocomplete=raw.get("autocomplete"),
        form_action=raw.get("form_action"),
        form_method=raw.get("form_method"),
        title=raw.get("title"),
        test_id=raw.get("test_id"),
        context_label=raw.get("context_label"),
        icon_label=raw.get("icon_label"),
        href=raw["href"],
        download=bool(raw.get("download")),
        in_nav=raw["in_nav"],
        in_form=raw["in_form"],
        in_modal=raw["in_modal"],
        region=raw.get("region"),
        kind=raw.get("kind"),
        container_type=raw.get("container_type"),
        controlled_surface=raw.get("controlled_surface"),
        component_label=raw.get("component_label"),
        fold=fold,
        bounding_box=BoundingBox(**raw["bounding_box"]),
        page_box=BoundingBox(**page_box) if page_box else None,
    )
    container_shape = strip_positional_selector(raw.get("container_selector") or "")
    item.container_key = _semantic_key(item.region, container_shape)
    item.item_id = _item_id(
        item.dom_instance_path,
        item.label,
        item.href,
        item.role,
        item.container_key,
    )
    item.group_id = group_signature(item)
    component_selector = raw.get("component_selector") or item.selector
    item.component_label = item.component_label or item.label
    item.component_key = _semantic_key(
        item.region,
        strip_positional_selector(component_selector),
        item.component_label,
    )
    item.control_key = _semantic_key(
        item.kind,
        item.region,
        item.label,
        item.href,
        item.container_key,
        item.component_key,
    )
    item.locator.update(
        {
            "role": item.role or ("link" if item.tag == "a" else "button"),
            "label": item.label,
            "href": item.href,
            "control_key": item.control_key,
        }
    )
    return item


def is_navigation_interactable(item: Interactable) -> bool:
    """Whether traversal may safely attempt this route-like control.

    Route owners with an href use direct same-origin navigation.  Link-like
    SPA controls without an href are executed as guarded DOM clicks.
    """
    href = (item.href or "").strip()
    if item.download or item.in_form or item.tag != "a" and item.role != "link":
        return False
    if not href:
        return True
    return not href.startswith(("#", "javascript:", "mailto:", "tel:", "sms:"))


_STRUCTURAL_CONTROL = re.compile(
    r"\b(categor(?:y|ies)|chart|column|date|display|dropdown|filter|layout|menu|metric|modal|"
    r"option|search|select|sort|table|view)\b",
    re.I,
)
_NAVIGATION_DISCLOSURE = re.compile(
    r"\b(show|see|view|load)\s+(more|less)\b|\b(expand|collapse)\b|"
    r"\b(menu|navigation|sidebar|guide)\b",
    re.I,
)


def probe_reason(item: Interactable) -> str | None:
    """Return why a non-link control is safe and useful to probe.

    This deliberately requires structural evidence. The disposable worker is
    defense in depth; it is not permission to click arbitrary application
    buttons or submit forms.
    """
    if item.href or item.download:
        return None
    if item.kind == "search" or item.input_type == "search" or item.role == "searchbox":
        return "focus_search"
    if item.in_form:
        return None
    if item.kind in {"tab", "disclosure", "select", "toggle"}:
        return f"semantic_{item.kind}"
    if item.aria_controls or item.aria_haspopup or item.aria_expanded is not None:
        return "aria_state_control"
    if item.role == "menuitem":
        return "menu_item"
    label = " ".join(
        filter(None, (item.text, item.aria_label, item.title, item.icon_label, item.context_label))
    )
    is_button = item.kind == "button" or item.tag == "button" or item.input_type == "button"
    shell_control = bool(
        item.in_nav
        or item.region in {"nav", "header", "aside"}
        or item.aria_controls
        or item.aria_expanded is not None
        or item.aria_pressed is not None
    )
    if is_button and shell_control and _NAVIGATION_DISCLOSURE.search(label):
        return "navigation_disclosure"
    if is_button and _STRUCTURAL_CONTROL.search(label):
        return "labelled_structural_button"
    if is_button and item.container_type == "table" and (
        item.context_label or item.icon_label
    ):
        return "table_control"
    return None


async def discover_interactables(
    page: Page,
    max_elements: int,
    max_scroll_steps: int = 0,
    max_inventory_elements: int = 200,
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
    navigation_count = 0
    inventory_count = 0

    def ingest(raw: list[dict], fold_index: int) -> None:
        nonlocal navigation_count, inventory_count
        for entry in raw:
            instance_path = entry.get("dom_instance_path") or entry["selector"]
            if instance_path in seen:
                continue
            item = _build_interactable(entry, fold_index)
            navigation = is_navigation_interactable(item)
            if navigation and navigation_count >= max_elements:
                continue
            if not navigation and inventory_count >= max_inventory_elements:
                continue
            seen[instance_path] = item
            order.append(instance_path)
            if navigation:
                navigation_count += 1
            else:
                inventory_count += 1

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
        observed: dict = await page.evaluate(
            _DISCOVER_JS, max_elements + max_inventory_elements
        )
        ingest(observed["items"], fold)

        page_height: float = observed["page_height"]
        reached_bottom = offset + viewport["height"] >= page_height
        quotas_full = (
            navigation_count >= max_elements
            and inventory_count >= max_inventory_elements
        )
        if quotas_full or fold >= max_scroll_steps or reached_bottom:
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

    # Window scrolling does not reveal controls inside independent navigation
    # rails, dialogs, menus, or listboxes. Sweep only those structural
    # containers and restore their original offsets; arbitrary app content is
    # deliberately excluded.
    container_count = await page.locator(
        'nav, [role="navigation"], [role="dialog"], dialog, '
        '[role="menu"], [role="listbox"]'
    ).count()
    for index in range(min(container_count, 20)):
        locator = page.locator(
            'nav, [role="navigation"], [role="dialog"], dialog, '
            '[role="menu"], [role="listbox"]'
        ).nth(index)
        try:
            metrics = await locator.evaluate(
                """(element) => ({
                    top: element.scrollTop,
                    height: element.clientHeight,
                    total: element.scrollHeight,
                })"""
            )
        except PlaywrightError:
            continue
        if metrics["height"] <= 0 or metrics["total"] <= metrics["height"] + 2:
            continue
        original_top = metrics["top"]
        component_steps = min(
            3,
            max(1, int((metrics["total"] - metrics["height"]) / metrics["height"]) + 1),
        )
        try:
            for step in range(1, component_steps + 1):
                target = min(
                    metrics["total"] - metrics["height"],
                    step * max(1, int(metrics["height"] * 0.9)),
                )
                await locator.evaluate(
                    """(element, top) => new Promise((resolve) => {
                        element.scrollTop = top;
                        requestAnimationFrame(() => requestAnimationFrame(resolve));
                    })""",
                    target,
                )
                observed = await page.evaluate(
                    _DISCOVER_JS, max_elements + max_inventory_elements
                )
                ingest(observed["items"], fold + step)
        finally:
            with contextlib.suppress(PlaywrightError):
                await locator.evaluate(
                    """(element, top) => { element.scrollTop = top; }""",
                    original_top,
                )

    items = [seen[instance_path] for instance_path in order]

    # Collapse overlapping responsive/icon/text renderers only when their
    # semantic context agrees. A single href-carrying copy can safely lend its
    # target to the chosen visible owner; ambiguous targets remain distinct.
    copy_groups: dict[tuple, list[Interactable]] = {}
    for item in items:
        box = item.page_box or item.bounding_box
        semantic_kind = "link" if item.tag == "a" or item.role == "link" else item.kind
        key = (
            item.label.strip().lower(),
            semantic_kind,
            item.region,
            item.container_key,
            round(box.x / 4),
            round(box.y / 4),
            round(box.width / 4),
            round(box.height / 4),
        )
        copy_groups.setdefault(key, []).append(item)

    collapsed: list[Interactable] = []
    emitted: set[str] = set()
    for item in items:
        if item.item_id in emitted:
            continue
        box = item.page_box or item.bounding_box
        semantic_kind = "link" if item.tag == "a" or item.role == "link" else item.kind
        key = (
            item.label.strip().lower(),
            semantic_kind,
            item.region,
            item.container_key,
            round(box.x / 4),
            round(box.y / 4),
            round(box.width / 4),
            round(box.height / 4),
        )
        group = copy_groups[key]
        targets = {member.href for member in group if member.href}
        if len(targets) > 1:
            collapsed.append(item)
            emitted.add(item.item_id)
            continue
        chosen = max(
            group,
            key=lambda member: (
                1 if member.href else 0,
                1 if member.tag == "a" else 0,
                1 if member.role == "link" else 0,
                -order.index(member.dom_instance_path),
            ),
        )
        if targets and not chosen.href:
            chosen.href = next(iter(targets))
            chosen.locator["href"] = chosen.href
            chosen.locator["adopted_href"] = True
            chosen.control_key = _semantic_key(
                chosen.kind,
                chosen.region,
                chosen.label,
                chosen.href,
                chosen.container_key,
                chosen.component_key,
            )
            chosen.locator["control_key"] = chosen.control_key
        chosen.locator["responsive_alias_count"] = len(group) - 1
        collapsed.append(chosen)
        emitted.update(member.item_id for member in group)
    return collapsed


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
        (item.tag == "a" or item.role == "link")
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
    if not href and (item.tag == "a" or item.role == "link"):
        # Some hydrated SPA shells publish the route after inventory. Re-read
        # the owner (or its descendant anchor) at execution time before
        # falling back to a click-only transition.
        with contextlib.suppress(PlaywrightError):
            await page.wait_for_function(
                """(selector) => {
                    const element = document.querySelector(selector);
                    return !!(
                        element?.getAttribute('href')
                        || element?.closest('a[href]')
                        || element?.querySelector('a[href]')
                    );
                }""",
                arg=item.selector,
                timeout=min(2_000, timeout_ms),
            )
        runtime_href = await locator.get_attribute("href")
        if not runtime_href:
            runtime_href = await locator.evaluate(
                """(element) =>
                    element.closest('a[href]')?.getAttribute('href')
                    || element.querySelector('a[href]')?.getAttribute('href')
                    || null
                """
            )
        if runtime_href and not runtime_href.startswith(
            ("#", "javascript:", "mailto:", "tel:")
        ):
            target = urljoin(page.url, runtime_href)
            origin = base_url or page.url
            source_url = page.url
            if is_same_origin(target, origin):
                try:
                    await page.goto(
                        target, timeout=timeout_ms, wait_until="domcontentloaded"
                    )
                    return
                except PlaywrightError:
                    if page.url != source_url:
                        return
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
        locator_count = await locator.count()
        if locator_count != 1:
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


async def rebind_interactable(
    page: Page,
    item: Interactable,
    *,
    max_elements: int,
    max_inventory_elements: int,
) -> Interactable | None:
    """Rediscover and uniquely bind one stale semantic control.

    CSS is intentionally absent from the match. A locator may change after
    hydration, while the source-local capability identity remains stable.
    """
    discovered = await discover_interactables(
        page,
        max_elements=max_elements,
        max_scroll_steps=0,
        max_inventory_elements=max_inventory_elements,
    )
    matches = [candidate for candidate in discovered if candidate.control_key == item.control_key]
    if not matches:
        matches = [
            candidate
            for candidate in discovered
            if candidate.label.strip().lower() == item.label.strip().lower()
            and candidate.kind == item.kind
            and candidate.region == item.region
            and (not item.href or candidate.href == item.href)
            and candidate.container_key == item.container_key
        ]
    return matches[0] if len(matches) == 1 else None


async def click_selector(
    page: Page,
    selector: str,
    *,
    timeout_ms: int,
    label: str | None = None,
    role: str | None = None,
    href: str | None = None,
    locator: dict | None = None,
) -> None:
    """Replay a stored path step without trusting an ambiguous CSS match."""
    recipe = locator or {}
    css = str(recipe.get("css") or selector or "")
    css_locator = page.locator(css) if css else None
    original_error: PlaywrightError | None = None
    try:
        if css_locator is not None and await css_locator.count() == 1:
            await css_locator.scroll_into_view_if_needed(timeout=timeout_ms)
            await css_locator.click(timeout=timeout_ms)
            return
    except PlaywrightError as exc:
        original_error = exc
    if href:
        target = urljoin(page.url, href)
        if is_same_origin(target, page.url):
            await page.goto(target, timeout=timeout_ms, wait_until="domcontentloaded")
            return
    if label and role:
        semantic = page.get_by_role(role, name=label, exact=True)
        if await semantic.count() == 1:
            await semantic.click(timeout=timeout_ms)
            return
    if css_locator is not None and await css_locator.count() == 1:
        try:
            await css_locator.click(timeout=timeout_ms, force=True)
            return
        except PlaywrightError:
            pass
    if original_error is not None:
        raise original_error
    raise PlaywrightError(f"Replay locator is ambiguous or missing: {css or label or role}")


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
