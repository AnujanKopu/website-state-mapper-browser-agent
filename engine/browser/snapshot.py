"""Page observation: stabilization, snapshot, DOM skeleton, and signals."""

from __future__ import annotations

import asyncio
import contextlib

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from engine.schemas import CaptureConfig, PageSignals, PageSnapshot

# Long polling and analytics frequently prevent network-idle forever. Two
# seconds still covers ordinary late fetches without charging every state the
# old five-second timeout; the configured DOM quiet period follows it.
_NETWORK_IDLE_TIMEOUT_MS = 2_000

# Builds a text-free skeleton of the *visible* DOM (tag + role tree, runs of
# more than 3 same-tag siblings truncated), extracts classification signals,
# and returns the other cheap DOM fields in one evaluate() round-trip.
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

  const labelOf = (el) => {
    const labelledBy = (el.getAttribute('aria-labelledby') || '')
      .split(/\\s+/)
      .filter(Boolean)
      .map((id) => document.getElementById(id)?.innerText || '')
      .join(' ')
      .trim();
    if (labelledBy) return labelledBy.slice(0, 160);
    if (el.id) {
      const explicit = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (explicit?.innerText) return explicit.innerText.trim().slice(0, 160);
    }
    return (el.getAttribute('aria-label') || el.getAttribute('alt')
      || el.getAttribute('title') || '').trim().slice(0, 160) || null;
  };
  const rectOf = (el) => {
    const rect = el.getBoundingClientRect();
    return { width: Math.round(rect.width), height: Math.round(rect.height) };
  };
  const forms = visibleAll('form').slice(0, 20).map((form) => ({
    label: labelOf(form),
    method: (form.getAttribute('method') || 'get').toLowerCase(),
    action: form.action || null,
    fields: Array.from(form.querySelectorAll('input, select, textarea'))
      .slice(0, 60)
      .map((field) => ({
      label: labelOf(field),
      tag: field.tagName.toLowerCase(),
      type: (field.getAttribute('type') || '').toLowerCase() || null,
      name: field.getAttribute('name'),
      required: field.required || field.getAttribute('aria-required') === 'true',
      autocomplete: field.getAttribute('autocomplete'),
      })),
  }));
  const visuals = visibleAll('table, svg, canvas, img, video, [role="img"]')
    .slice(0, 60)
    .map((el) => ({
      kind: el.getAttribute('role') === 'img' ? 'graphic' : el.tagName.toLowerCase(),
      label: labelOf(el)
        || el.querySelector?.('caption, title, desc')?.textContent?.trim().slice(0, 160)
        || null,
      ...rectOf(el),
    }));
  const headings = visibleAll('h1, h2, h3, h4, h5, h6, [role="heading"]')
    .slice(0, 80)
    .map((el) => ({
      level: Number(el.getAttribute('aria-level')) || Number(el.tagName.slice(1)) || null,
      text: (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 240),
    }))
    .filter((item) => item.text);

  return {
    title: document.title || '',
    visible_text: document.body ? document.body.innerText : '',
    skeleton: document.body ? skeletonOf(document.body, 0) : '',
    evidence: {
      page: {
        language: document.documentElement.lang || null,
        canonical_url: document.querySelector('link[rel="canonical"]')?.href || null,
        viewport: {
          width: window.innerWidth,
          height: window.innerHeight,
          dpr: window.devicePixelRatio,
        },
        document: {
          width: document.documentElement.scrollWidth,
          height: document.documentElement.scrollHeight,
        },
      },
      forms,
      visuals,
    },
    text_evidence: { headings },
    signals: {
      modal_open: visibleAll('[role="dialog"], dialog[open], [aria-modal="true"]').length > 0,
      password_fields: visibleAll('input[type="password"]').length,
      username_fields: visibleAll([
        'input[type="email"]',
        'input[autocomplete="username"]',
        'input[name*="user" i]',
        'input[name*="login" i]',
      ].join(', ')).length,
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
    observed: dict = await page.evaluate(_OBSERVE_JS)
    visible_text: str = observed["visible_text"]
    if len(visible_text) > config.max_visible_text_chars:
        visible_text = visible_text[: config.max_visible_text_chars]

    return PageSnapshot(
        url=page.url,
        title=observed["title"],
        visible_text=visible_text,
        html=await page.content() if config.save_dom_snapshots else "",
        screenshot_png=await page.screenshot(
            full_page=config.full_page_screenshot,
            caret="hide",
        ),
        dom_skeleton=observed["skeleton"],
        signals=PageSignals(**observed["signals"]),
        evidence=observed["evidence"],
        text_evidence=observed["text_evidence"],
    )
