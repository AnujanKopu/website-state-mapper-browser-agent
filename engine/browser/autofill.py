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
    """Fill a username-first, password-only, or combined authentication step."""
    username_val = credentials.username or ""
    password_val = credentials.password or ""
    if not username_val and not password_val:
        return False

    async def visible_first(selectors: list[str]):
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                await locator.wait_for(state="visible", timeout=500)
                return locator
            except PlaywrightError:
                continue
        return None

    username = await visible_first(
        [
            "input[type=email]",
            "input[name*=email i]",
            "input[id*=email i]",
            "input[placeholder*=email i]",
            "input[name*=user i]",
            "input[id*=user i]",
            "input[name*=login i]",
            "input[id*=login i]",
            "input[type=text]",
        ]
    )
    password = await visible_first(["input[type=password]"])
    if username is None and password is None:
        return False

    try:
        if username is not None and username_val:
            await username.fill(username_val, timeout=_FILL_TIMEOUT_MS)
        if password is not None and password_val:
            await password.fill(password_val, timeout=_FILL_TIMEOUT_MS)
    except PlaywrightError:
        return False

    submit = await visible_first(["input[type=submit]", "button[type=submit]"])
    if submit is not None:
        try:
            await submit.click(timeout=timeout_ms)
            return True
        except PlaywrightError:
            pass

    try:
        buttons = await page.locator("button").all()
        for button in buttons:
            try:
                text = (await button.inner_text(timeout=500)).strip()
                if _SUBMIT_TEXT.search(text):
                    await button.click(timeout=timeout_ms)
                    return True
            except PlaywrightError:
                continue
    except PlaywrightError:
        pass
    return False
