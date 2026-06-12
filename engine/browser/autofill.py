"""Heuristic credential autofill for auth-wall forms.

Fills an email/username and password field, then submits the form by clicking
the most likely login/submit button. Never touches payment forms.

Used by the Explorer's auth-gate handler before pausing for user intervention.
Credentials are passed in memory only and are never written to disk or logs.
"""

from __future__ import annotations

import re

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from engine.schemas import Credentials

# Patterns for identifying the username/email input.
_USERNAME_NAME = re.compile(r"\b(email|user(name)?|login|account)\b", re.I)
_USERNAME_PLACEHOLDER = re.compile(r"\b(email|user(name)?|login)\b", re.I)

# Text on auth-submit buttons.
_SUBMIT_TEXT = re.compile(
    r"\b(log\s?in|sign\s?in|log\s?on|sign\s?on|continue|submit|enter|access)\b",
    re.I,
)

_FILL_TIMEOUT_MS = 3_000
_CLICK_TIMEOUT_MS = 5_000


async def autofill_auth_form(
    page: Page,
    credentials: Credentials,
    *,
    timeout_ms: int = _CLICK_TIMEOUT_MS,
) -> bool:
    """Attempt to fill and submit the login form on `page`.

    Searches for an email/username field and a password field, fills them, and
    clicks the most plausible submit button. Returns True if a submit click was
    attempted (regardless of whether authentication succeeded — the caller must
    re-observe the page to verify).  Returns False if the expected fields could
    not be found.
    """
    username_val = credentials.username or ""
    password_val = credentials.password or ""
    if not username_val and not password_val:
        return False

    # --- find password field ---
    password_handle = None
    try:
        password_handle = page.locator("input[type=password]").first
        await password_handle.wait_for(state="visible", timeout=_FILL_TIMEOUT_MS)
    except PlaywrightError:
        return False  # no password field → not an auth form

    # --- find username/email field ---
    username_handle = None
    # Try type=email first.
    for locator_expr in [
        "input[type=email]",
        "input[name*=email i]",
        "input[id*=email i]",
        "input[placeholder*=email i]",
        "input[name*=user i]",
        "input[id*=user i]",
        "input[name*=login i]",
        "input[id*=login i]",
        "input[type=text]",  # last resort: first visible text input
    ]:
        try:
            handle = page.locator(locator_expr).first
            await handle.wait_for(state="visible", timeout=500)
            username_handle = handle
            break
        except PlaywrightError:
            continue

    # --- fill fields ---
    try:
        if username_handle and username_val:
            await username_handle.fill(username_val, timeout=_FILL_TIMEOUT_MS)
        if password_val:
            await password_handle.fill(password_val, timeout=_FILL_TIMEOUT_MS)
    except PlaywrightError:
        return False

    # --- find and click submit button ---
    submitted = False
    # Try: explicit submit input/button in the form context.
    for locator_expr in [
        "input[type=submit]",
        "button[type=submit]",
    ]:
        try:
            handle = page.locator(locator_expr).first
            await handle.wait_for(state="visible", timeout=500)
            await handle.click(timeout=timeout_ms)
            submitted = True
            break
        except PlaywrightError:
            continue

    # Fallback: any button whose text matches common auth-submit patterns.
    if not submitted:
        try:
            buttons = await page.locator("button").all()
            for btn in buttons:
                try:
                    text = (await btn.inner_text(timeout=500)).strip()
                    if _SUBMIT_TEXT.search(text):
                        await btn.click(timeout=timeout_ms)
                        submitted = True
                        break
                except PlaywrightError:
                    continue
        except PlaywrightError:
            pass

    return submitted
